#!/usr/bin/env python3
"""
scripts/seed_technocore_memory.py — Embed documents from the configured optional connector into MemoryItem.

Sources (derived from TECHNOCORE_BASE_URL):
  - ${TECHNOCORE_BASE_URL}/skill.md    -> source=technocore-skill/md
  - ${TECHNOCORE_BASE_URL}/llms.txt    -> source=technocore-llms
  - ${TECHNOCORE_BASE_URL}/patterns.md -> source=technocore-patterns

Each record:
  category = "technocore"
  status   = ACTIVE  (MemoryStatus.ACTIVE)
  content  = full document text (Text column; no 8 KB truncation)

Idempotent: update the record for an existing source; otherwise create it.

Usage:
  python scripts/seed_technocore_memory.py
  DATABASE_URL=postgresql+asyncpg://... python scripts/seed_technocore_memory.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# repo root -> packages importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

import httpx
from sqlalchemy import select

from observability.config import settings
from observability.db import async_session_factory
from observability.models import MemoryItem, MemoryStatus

if not settings.TECHNOCORE_ENABLED or not settings.TECHNOCORE_BASE_URL:
    print("Technocore disabled (TECHNOCORE_ENABLED=false or TECHNOCORE_BASE_URL empty), skipping seed.")
    sys.exit(0)

BASE = settings.TECHNOCORE_BASE_URL.rstrip("/")
DOCS: list[tuple[str, str]] = [
    (f"{BASE}/skill.md", "technocore-skill/md"),
    (f"{BASE}/llms.txt", "technocore-llms"),
    (f"{BASE}/patterns.md", "technocore-patterns"),
]


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    # httpx automatically decodes charset; plain text UTF-8
    text = resp.text
    # Text column — do NOT truncate over 8 KB (intentional)
    return text


async def seed() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "lumi-seed/1.0"}) as client:
        fetched: dict[str, str] = {}
        for url, source in DOCS:
            print(f"fetch {url} ...", flush=True)
            try:
                txt = await fetch_text(client, url)
            except Exception as exc:
                print(f"  ! fetch failed {url}: {exc}", file=sys.stderr)
                raise
            print(f"  -> {len(txt.encode('utf-8'))} bytes, {len(txt)} chars [source={source}]")
            fetched[source] = txt

    # Database write
    async with async_session_factory() as session:
        for source, content in fetched.items():
            # idempotent upsert: search by source
            stmt = select(MemoryItem).where(MemoryItem.source == source)
            res = await session.execute(stmt)
            existing = res.scalars().first()

            if existing is not None:
                existing.content = content
                existing.category = "technocore"
                existing.status = MemoryStatus.ACTIVE.value
                existing.verification_status = "unverified"
                print(f"update MemoryItem source={source} id={existing.id}")
            else:
                item = MemoryItem(
                    content=content,
                    source=source,
                    category="technocore",
                    status=MemoryStatus.ACTIVE.value,
                    verification_status="unverified",
                    confidence=1.0,
                )
                session.add(item)
                print(f"insert MemoryItem source={source}")

        await session.commit()

    print("done. 3 records written as ACTIVE (no truncate).")


if __name__ == "__main__":
    asyncio.run(seed())
