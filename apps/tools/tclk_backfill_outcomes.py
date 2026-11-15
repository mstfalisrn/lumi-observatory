#!/usr/bin/env python3
"""Close audit rows that were decided `accept` but never acted on.

Rows written before the accept path recorded its own exits carry an empty outcome, so the
funnel counted them as accepted work that vanished. They cannot be re-attributed now —
the reason was never stored — so they are marked `unrecorded`, which is the honest
label and keeps an empty outcome meaning "still in flight".

    python apps/tools/tclk_backfill_outcomes.py            # count only
    python apps/tools/tclk_backfill_outcomes.py --apply    # write `unrecorded`

Only rows older than one day are touched, so a live accept is never mislabelled.
Reads the scheduler's database URL from the environment (``DATABASE_URL``); it never
prints a credential.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

COUNT_SQL = """
select count(*) from tclk_offer_audits
where decision = 'accept' and coalesce(outcome, '') = '' and created_at < now() - interval '1 day'
"""
APPLY_SQL = """
update tclk_offer_audits
set outcome = 'unrecorded', answer = ''
where decision = 'accept' and coalesce(outcome, '') = '' and created_at < now() - interval '1 day'
"""


async def main(apply: bool) -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            pending = (await conn.execute(text(COUNT_SQL))).scalar_one()
            print(f"pending accept rows without outcome: {pending}")
            if not apply:
                print("dry run — pass --apply to mark them unrecorded")
                return 0
            if pending == 0:
                return 0
            result = await conn.execute(text(APPLY_SQL))
            print(f"marked unrecorded: {result.rowcount}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="mark decided-but-unacted audit rows")
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
