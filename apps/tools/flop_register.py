#!/usr/bin/env python3
"""FLOP / Technocore identity bootstrap — the first thing a fresh install runs.

Generates (or loads) the agent's Ed25519 key, derives its ``did:key``, publishes
the identity note on technocore.chat (``/kv/did-<shard>/<key>``), claims the
devnet faucet drip, then verifies both by reading them back.

The private key never leaves the key file (0600, outside the repo by default) —
only the DID and signatures are ever sent to the venue.

Usage (from the repo root; inside the scheduler container the repo is at /app):

  python apps/tools/flop_register.py                  # register + verify
  python apps/tools/flop_register.py --check          # read-only status report
  python apps/tools/flop_register.py --name "LUMI"    # profile name in the note
  python apps/tools/flop_register.py --no-faucet      # skip the drip claim
  python apps/tools/flop_register.py --force-note     # overwrite an existing note
  python apps/tools/flop_register.py --key-path PATH  # custom key location

Exit codes: 0 ok / 1 error / 3 faucet rate-limited (registration itself is fine).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx

DEFAULT_NAME = os.environ.get("LUMI_AGENT_NAME", "LUMI")
FAUCET_ROOM = "faucet"
DRIP_MARK = "faucet drip"


def base_url() -> str:
    return os.environ.get("TECHNOCORE_BASE_URL", "https://technocore.chat").rstrip("/")


def default_key_path() -> str:
    """Key file resolution order: explicit env -> repo ./secrets -> container mount."""
    for env in ("TECHNOCORE_ED25519_KEY_PATH", "TECHNOCORE_KEY_PATH", "TECHNOCORE_KEY_HOST_PATH"):
        val = os.environ.get(env)
        if val:
            return val
    local = ROOT / "secrets" / "did.ed25519"
    if local.is_file():
        return str(local)
    return "/run/secrets/technocore/did.ed25519"


def fingerprint(did: str) -> str:
    """First 16 lowercase hex chars of SHA-256(did:key string) — the venue's rule."""
    return hashlib.sha256(did.encode()).hexdigest()[:16]


def note_paths(did: str) -> tuple[str, str]:
    """(sharded, legacy) note paths for a DID, per technocore.chat/llms.txt IDENTITY."""
    fp = fingerprint(did)
    return f"/kv/did-{fp[:2]}/{fp[2:]}", f"/kv/did/{fp}"


async def kv_read(client: httpx.AsyncClient, path: str) -> tuple[int, str]:
    try:
        r = await client.get(base_url() + path, headers={"User-Agent": "lumi-register/1.0"})
        return r.status_code, r.text
    except Exception as e:  # network hiccup — caller treats non-200 as absent
        return 0, f"{type(e).__name__}: {e}"


async def kv_write(
    client: httpx.AsyncClient, path: str, value: str, *, if_absent: bool = True
) -> tuple[int, str]:
    body: dict = {"value": value}
    if if_absent:
        body["if_absent"] = True
    try:
        r = await client.post(
            base_url() + path,
            json=body,
            headers={"User-Agent": "lumi-register/1.0"},
        )
        return r.status_code, r.text[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def note_body(did: str, name: str) -> str:
    return "\n".join(
        [
            did,
            f"agent: {name}",
            "venue: technocore.chat",
            f"registered: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        ]
    )


async def register_note(
    client: httpx.AsyncClient, did: str, name: str, *, force: bool
) -> tuple[str, str]:
    """Publish the DID note. Returns (status, detail)."""
    sharded, legacy = note_paths(did)
    code, body = await kv_read(client, sharded)
    existing = body if code == 200 else ""
    if code != 200:
        code2, body2 = await kv_read(client, legacy)
        if code2 == 200:
            existing = body2

    if existing and did in existing and not force:
        return "exists", f"{sharded} (already published, untouched)"

    value = note_body(did, name)
    code, detail = await kv_write(client, sharded, value, if_absent=not force)
    if code == 409:
        # someone/something wrote it between our read and write — re-read and accept ours
        code2, body2 = await kv_read(client, sharded)
        if code2 == 200 and did in body2:
            return "exists", f"{sharded} (raced, but contains our DID)"
        return "conflict", f"{sharded} exists with foreign content — re-run with --force-note"
    if code != 200:
        return "failed", f"{sharded} write http={code} {detail!r}"

    code3, body3 = await kv_read(client, sharded)
    if code3 == 200 and did in body3:
        return "published", sharded
    return "unverified", f"{sharded} (write ok, readback http={code3})"


async def faucet_status(client: httpx.AsyncClient, did: str) -> dict:
    """Count our drips / refusals from the faucet room; read the balance note."""
    out: dict = {"drips": 0, "last_drip": "", "refusal": "", "balance": ""}
    try:
        r = await client.get(
            f"{base_url()}/r/{FAUCET_ROOM}?format=json&limit=200",
            headers={"User-Agent": "lumi-register/1.0"},
        )
        if r.status_code == 200:
            for m in r.json().get("messages") or []:
                text = str(m.get("text") or "")
                if did in text and DRIP_MARK in text:
                    out["drips"] += 1
                    out["last_drip"] = str(m.get("ts") or "")
                elif did in text and ("one drip per hour" in text or "wait a little bit longer" in text):
                    out["refusal"] = str(m.get("ts") or "")
    except Exception:
        pass
    fp = fingerprint(did)
    code, body = await kv_read(client, f"/kv/faucet/{fp}")
    if code == 200:
        lines = [ln for ln in body.splitlines() if ln and not ln.startswith("!!")]
        out["balance"] = " ".join(lines)[-200:]
    return out


async def claim_faucet(client: httpx.AsyncClient, connector, did: str, *, wait_s: int) -> tuple[str, str]:
    """Post a signed drip claim, then poll the room briefly for the issuer's answer."""
    st = await faucet_status(client, did)
    if st["last_drip"]:
        age = time.time() - _ts_epoch(st["last_drip"])
        if age < 3600:
            return "skipped", f"last drip {int(age / 60)} min ago (issuer: one per hour)"

    sharded, _ = note_paths(did)
    text = (
        f"FLOP testnet faucet claim. DID: {did}. "
        f"DID note at {sharded}. Requesting testnet dFLOP drip."
    )
    url = connector.build_signed_get_url(FAUCET_ROOM, text)
    try:
        r = await client.get(url, headers={"User-Agent": "lumi-register/1.0"})
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return "failed", f"claim http={r.status_code} {r.text[:160]!r}"

    deadline = time.time() + max(wait_s, 0)
    while time.time() < deadline:
        await asyncio.sleep(10)
        st = await faucet_status(client, did)
        if st["last_drip"] and _ts_epoch(st["last_drip"]) >= _ts_epoch(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")) - wait_s - 60:
            return "dripped", f"drip seen at {st['last_drip']} — balance note: {st['balance'] or 'n/a'}"
        if st["refusal"]:
            return "rate-limited", f"issuer says wait ({st['refusal']})"
    return "posted", "claim posted — the drip lands within minutes (re-run --check to see it)"


def _ts_epoch(ts: str) -> float:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
    except Exception:
        return 0.0


async def main_async(args: argparse.Namespace) -> int:
    from connectors.technocore import TechnocoreConnector

    key_path = args.key_path or default_key_path()
    connector = TechnocoreConnector(base_url(), ed25519_key_path=key_path)

    if args.check:
        did = connector.load_key(key_path)
        if not did:
            print("STATUS=no-key")
            print(f"KEY={key_path}")
            print("hint: run without --check to generate the key and register")
            await connector.aclose()
            return 1
        sharded, legacy = note_paths(did)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            code, body = await kv_read(client, sharded)
            if code != 200:
                code, body = await kv_read(client, legacy)
            note = "published" if code == 200 and did in body else "missing"
            st = await faucet_status(client, did)
        print(f"DID={did}")
        print(f"KEY={key_path}")
        print(f"NOTE={sharded} ({note})")
        print(f"FAUCET=drips:{st['drips']} last:{st['last_drip'] or 'never'} balance:{st['balance'] or 'n/a'}")
        await connector.aclose()
        return 0

    kp = Path(key_path)
    if kp.is_file() and kp.stat().st_size not in (32, 64):
        # a placeholder (e.g. an empty file created so docker bind-mounts a file
        # instead of a directory) — replace it with a real key
        print(f"KEY=placeholder at {key_path} (size {kp.stat().st_size}) — generating a real key")
        kp.unlink()
    generated = not kp.is_file()
    did, path = connector.load_or_generate_key(key_path)
    if not did:
        print("ERROR: key could not be loaded or generated", file=sys.stderr)
        await connector.aclose()
        return 1
    print(f"DID={did}")
    print(f"KEY={path or key_path} ({'generated' if generated else 'loaded'})")

    NOTE_VERIFIED = {"exists", "published"}

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        status, detail = await register_note(client, did, args.name, force=args.force_note)
        print(f"NOTE={status}: {detail}")
        if status not in NOTE_VERIFIED:
            # no note, foreign note, or a write we could not read back — this is
            # NOT a registration; the caller must not report success
            print(f"OUTCOME=note-{status}")
            await connector.aclose()
            return 1

        if args.no_faucet:
            print("FAUCET=skipped (--no-faucet)")
            fstatus = "skipped"
        else:
            fstatus, fdetail = await claim_faucet(client, connector, did, wait_s=args.wait)
            print(f"FAUCET={fstatus}: {fdetail}")

    print("NEXT=set LUMI_AGENT_DID in .env (the wizard does this automatically) and start the stack")
    await connector.aclose()
    if fstatus == "rate-limited":
        print("OUTCOME=registered-faucet-wait")
        return 3
    if fstatus == "failed":
        print("OUTCOME=registered-faucet-failed")
        return 4
    print("OUTCOME=registered")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="flop_register",
        description="FLOP / Technocore identity bootstrap: key, DID note, faucet drip.",
    )
    ap.add_argument("--check", action="store_true", help="read-only status report, never writes")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"profile name in the note (default: {DEFAULT_NAME})")
    ap.add_argument("--key-path", default="", help="Ed25519 key file (default: env chain, then ./secrets/did.ed25519)")
    ap.add_argument("--no-faucet", action="store_true", help="skip the faucet drip claim")
    ap.add_argument("--force-note", action="store_true", help="overwrite an existing DID note")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for the drip answer (default 60)")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
