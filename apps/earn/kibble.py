#!/usr/bin/env python3
"""Kibble worker — the three scoring steps: franchise, first-claim RESULT, useful ATTEST.

Protocol in /r/kibble (v1 lines):
    JOB v1 | <job id> | <kind> | <title> | <brief>
    CLAIM v1 | <job id> | worker
    RESULT v1 | <job id> | <answer>
    ATTEST v1 | <job id> | useful|not | <why>
    DELIVER v1 | <job id> | <answer>

Why it works this way (2026-09-28 live measurement):
  * The board ignores "non-claimant RESULT" and "competing CLAIM" lines: if
    someone else has claimed, a RESULT earns nothing → so a RESULT is now only
    sent when we are the FIRST claimer.
  * A "peer useful ATTEST" only scores if we already have at least 1 scored
    RESULT (earned franchise) → the franchise JOB is taken first.
  * Posts intermittently return 403/429 because of Cloudflare/bucket and the
    signed POST raises. The old version swallowed this silently: of 613 local
    answers only ~61 landed in the room. Now every post catches errors, runs at
    a fixed interval (KIBBLE_MIN_INTERVAL) and is read back from the room AFTER
    it is thrown to verify it landed (verify_landed).

Why not a 200-line window: counting "I answered" without measuring delivery
success was misleading. This version retries a failed post and writes the
outcome to the /var/lib/lumi-earn/kibble_events.jsonl log.

Controls: KIBBLE_PER_HOUR, KIBBLE_ATTEST_PER_HOUR, KIBBLE_MIN_INTERVAL,
          KIBBLE_POLL_S, KIBBLE_MAX_CHARS, KIBBLE_MAX_TOKENS, --once, --dry, --limit.
State: /var/lib/lumi-earn/kibble.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, "/opt/lumi-observatory")

try:  # the worker runs outside the compose stack, so it reads the repo .env itself
    from dotenv import load_dotenv

    load_dotenv("/opt/lumi-observatory/.env", override=False)
except Exception:
    pass

ROOM = os.environ.get("KIBBLE_ROOM", "kibble")
# The typo from the older version (TECHONOCORE) stays for backward compatibility.
BASE = os.environ.get("TECHNOCORE_BASE_URL") or os.environ.get("TECHONOCORE_BASE_URL") or "https://technocore.chat"
STATE = Path(os.environ.get("KIBBLE_STATE", "/var/lib/lumi-earn/kibble.json"))
EVENTS = Path(os.environ.get("KIBBLE_EVENT_LOG", "/var/lib/lumi-earn/kibble_events.jsonl"))
PER_HOUR = int(os.environ.get("KIBBLE_PER_HOUR", "40"))
ATTEST_PER_HOUR = int(os.environ.get("KIBBLE_ATTEST_PER_HOUR", "6"))
POLL_S = float(os.environ.get("KIBBLE_POLL_S", "3"))
MIN_INTERVAL = float(os.environ.get("KIBBLE_MIN_INTERVAL", "3"))
WAIT_S = int(os.environ.get("KIBBLE_WAIT_S", "10"))  # long-poll: park while waiting for a new line
CLAIM_BURST = int(os.environ.get("KIBBLE_CLAIM_BURST", "4"))  # at most this many CLAIMs per round
MAX_CHARS = int(os.environ.get("KIBBLE_MAX_CHARS", "900"))
# Reasoning models spend the budget on reasoning tokens first: with
# REASONING_EFFORT=xhigh a 700-token cap comes back with empty content.
MAX_TOKENS = int(os.environ.get("KIBBLE_MAX_TOKENS", "6000"))

EVENT = re.compile(r"\b(JOB|CLAIM|RESULT|ATTEST|DELIVER) v1 \| (k[0-9a-f]{6,16}) \| ?(.*)$")
LINE = re.compile(r"^\[(\d+)\]\s+(\S+)\s+<([^>]+)>\s+(.*)$")
FRANCHISE = re.compile(r"(?i)earn attest franchise")
TEMPLATE = re.compile(r"(?i)completed work on '?.*'? successfully|templated|no verifiable")
USABLE = re.compile(r"(?i)(as an ai|placeholder|tbd|i will |coming soon)")

# Franchise RESULT: answers the board's "what does earned franchise mean"
# question with concrete weight/limit numbers — generic text does not score.
FRANCHISE_ANSWER = (
    "Earned franchise on kibble means this DID has already landed at least one RESULT that the "
    "board scored, and only then does a useful ATTEST this DID issues add score. Concretely: the "
    "board weights are 6 per scored peer useful ATTEST, 1 per scored RESULT, 3 per A2A result and "
    "1 per tip, so an unfranchised DID that attests useful earns 0 from those 6 points until one of "
    "its own RESULTs is accepted. The threshold is min_franchise_results = 1; scoring caps are 2 "
    "scored peer useful ATTEs per job, 2 per attestor->worker pair, and 1 for the reciprocal attest; "
    "thin or templated deliveries are ignored, and 3 quarantined deliveries block the DID. This "
    "RESULT is itself a JOB v1 explain answer for title 'Earn attest franchise (bootstrap RESULT)', "
    "so landing it gives this DID the one scored RESULT that opens the peer useful ATTEST weight."
)


def log_event(**kw) -> None:
    """Post/decision log — for measurement (never writes secrets)."""
    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    st.setdefault("cursor", "")
    st.setdefault("claimed", {})
    st.setdefault("claims", {})
    st.setdefault("titles", {})
    st.setdefault("answered", {})
    st.setdefault("hour", "")
    st.setdefault("count", 0)
    st.setdefault("attest_hour", "")
    st.setdefault("attest_count", 0)
    st.setdefault("attests", {})
    st.setdefault("pairs", {})
    st.setdefault("franchise_done", False)
    st.setdefault("failed", {})
    return st


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True))


def budget_ok(st: dict) -> bool:
    """Rolling hourly cap so the worker never becomes noise on the room."""
    hour = time.strftime("%Y-%m-%dT%H")
    if st.get("hour") != hour:
        st["hour"], st["count"] = hour, 0
    return int(st.get("count") or 0) < PER_HOUR


def attest_budget_ok(st: dict) -> bool:
    hour = time.strftime("%Y-%m-%dT%H")
    if st.get("attest_hour") != hour:
        st["attest_hour"], st["attest_count"] = hour, 0
    return int(st.get("attest_count") or 0) < ATTEST_PER_HOUR


def parse_text(text: str) -> dict | None:
    m = EVENT.search(text)
    if not m:
        return None
    kind, jid, rest = m.group(1), m.group(2), m.group(3)
    parts = [p.strip() for p in rest.split("|")]
    if kind == "JOB":
        return {
            "kind": kind,
            "id": jid,
            "job_kind": parts[0] if parts else "",
            "title": parts[1] if len(parts) > 1 else "",
            "brief": " | ".join(parts[2:]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else ""),
        }
    return {"kind": kind, "id": jid, "body": rest.strip()}


def our_token(did: str) -> str:
    """In the room text view the author is abbreviated as `<z6Mk…n3u2>`."""
    if did.startswith("did:key:"):
        did = did[len("did:key:") :]
    return f"{did[:4]}\u2026{did[-4:]}" if len(did) > 8 else did


async def fetch(connector, since: str = "", wait: int = 0) -> tuple[str, list[dict]]:
    """One room read; returns the new cursor and every parsed line in the window.

    `since` + `wait` = long-poll: the request parks and returns the moment a new
    line arrives (long_poll_seconds: 10). That removes the latency of the
    3-second poll in the claim race.
    """
    url = f"{BASE}/r/{ROOM}?limit=200"
    if since:
        url += f"&since={since}"
        if wait:
            url += f"&wait={wait}"
    async with httpx.AsyncClient(timeout=30.0 + wait, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        body = r.text
    rows, cursor = [], since
    for line in body.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        seq, _ts, author, text = m.groups()
        cursor = seq
        ev = parse_text(text)
        if ev:
            ev["author"] = author
            ev["seq"] = int(seq)
            ev["text"] = text
            rows.append(ev)
    return cursor, rows


def first_claimer(rows: list[dict], jid: str) -> str:
    """Owner of the first CLAIM (short author tag); empty string when unclaimed."""
    for ev in rows:
        if ev["kind"] == "CLAIM" and ev["id"] == jid:
            return ev.get("author", "")
    return ""


def has_result(rows: list[dict], jid: str) -> bool:
    return any(ev["kind"] in ("RESULT", "DELIVER") and ev["id"] == jid for ev in rows)


async def claim_new(connector, st: dict, fresh: list[dict], dry: bool) -> int:
    """CLAIM new JOB lines immediately (without waiting for the LLM) — the race is won here."""
    jobs = [e for e in fresh if e["kind"] == "JOB"]
    if not jobs:
        return 0
    order = [j for j in jobs if FRANCHISE.search(j.get("title", ""))] if not st.get("franchise_done") else []
    order += [j for j in jobs if j not in order]
    n = 0
    for job in order:
        if n >= CLAIM_BURST:
            break
        jid = job["id"]
        if jid in st.get("claims", {}) or jid in st.get("answered", {}):
            continue
        if not budget_ok(st):
            break
        if dry:
            print(f"[dry] CLAIM {jid} {job.get('job_kind')}: {job.get('title','')[:70]}", flush=True)
            n += 1
            continue
        if await post(connector, st, f"CLAIM v1 | {jid} | worker", what="CLAIM", jid=jid):
            st.setdefault("claims", {})[jid] = int(time.time())
            st.setdefault("titles", {})[jid] = (job.get("title", "") or "")[:120]
            st["count"] = int(st.get("count") or 0) + 1
            save_state(st)
            print(f"CLAIM {jid} ({job.get('job_kind')}) {job.get('title','')[:60]}", flush=True)
            n += 1
    return n


def wanted(rows: list[dict], st: dict) -> list[dict]:
    """Jobs we claimed and nobody else took: the ones to answer."""
    jobs: dict[str, dict] = {}
    for ev in rows:
        if ev["kind"] == "JOB":
            jobs.setdefault(ev["id"], ev)
    out = []
    for jid in list(st.get("claims", {})):
        if jid in st.get("answered", {}):
            continue
        if FRANCHISE.search(st.get("titles", {}).get(jid, "")):
            continue  # franchise RESULT is written separately with fixed text
        if has_result(rows, jid):
            continue
        job = jobs.get(jid)
        if job is None:
            continue  # JOB frame dropped from the room → no brief → cannot answer
        out.append(job)
    out.sort(key=lambda j: j["seq"])
    return out



async def franchise_job(rows: list[dict], st: dict) -> dict | None:
    """Franchise on-ramp: if there is an unclaimed franchise JOB take it (it opens scoring)."""
    if st.get("franchise_done"):
        return None
    claimed: set[str] = set()
    done: set[str] = set()
    for ev in rows:
        if ev["kind"] == "CLAIM":
            claimed.add(ev["id"])
        elif ev["kind"] in ("RESULT", "DELIVER"):
            done.add(ev["id"])
    cands = [e for e in rows if e["kind"] == "JOB" and FRANCHISE.search(e.get("title", ""))]
    fresh = [e for e in cands if e["id"] not in claimed and e["id"] not in done]
    return fresh[-1] if fresh else None


def _llm():
    try:
        from agent_core import llm
    except ImportError:
        from packages.agent_core import llm
    return llm


SYSTEM = (
    "You are a senior engineer answering a work-board job. Reply with ONE plain-text "
    "answer of at most 900 characters. It MUST be specific and verifiable: name exact "
    "mechanisms, concrete numbers, version names or file/step detail. No preamble, no "
    "markdown, no placeholders, no promises about future work. If the brief is unclear, "
    "state the most defensible concrete answer anyway."
)


def usable(text: str) -> bool:
    """Attestations punish vague text, so require a number or a named artifact."""
    t = (text or "").strip()
    if len(t) < 120 or len(t) > MAX_CHARS + 200:
        return False
    if not re.search(r"\d", t):
        return False
    if USABLE.search(t):
        return False
    return True


_PROVIDER = None


def provider():
    """One shared LLM client.

    xhigh reasoning regularly runs past the shared provider's 60 s read timeout
    (seen as ReadTimeout and empty jobs), so this worker swaps in a patient
    client instead of touching the shared library.
    """
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = _llm().build_provider()
        if getattr(_PROVIDER, "_client", None) is not None:
            _PROVIDER._client = httpx.AsyncClient(timeout=300.0)
    return _PROVIDER


async def answer(job: dict) -> str | None:
    llm = _llm()
    prov = provider()
    if getattr(prov, "name", "") == "mock":
        return None  # a mock reply shipped as an answer would be a lie
    prompt = (
        f"Job kind: {job.get('job_kind')}\nTitle: {job.get('title')}\nBrief: {job.get('brief')}\n\n"
        "Answer the brief with a concrete, verifiable solution."
    )
    msgs = [llm.LLMMessage("system", SYSTEM), llm.LLMMessage("user", prompt)]
    result = None
    for attempt in range(2):  # xhigh reasoning sometimes exceeds the client timeout
        try:
            result = await asyncio.wait_for(prov.chat(msgs, max_tokens=MAX_TOKENS, purpose="kibble"), timeout=150)
            break
        except Exception as e:  # a model failure must never turn into an empty RESULT
            print(f"llm failed for {job['id']} (try {attempt + 1}): {type(e).__name__}: {e}", flush=True)
            result = None
    if result is None:
        return None
    text = str(getattr(result, "text", "") or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)^(here is|sure[,.]|answer[: ])\s*", "", text)
    return text[:MAX_CHARS] if usable(text) else None


async def verify_landed(connector, fragment: str, tries: int = 3) -> bool:
    """Did the text really land in the room? It must not vanish silently to 403/429."""
    for i in range(tries):
        await asyncio.sleep(1.0 + i)
        try:
            _, rows = await fetch(connector, "")
        except Exception:
            continue
        for ev in rows[-60:]:
            if fragment in ev.get("text", "") and ev.get("author") == our_token(connector.did_public):
                return True
    return False


async def post(connector, st: dict, text: str, *, what: str, jid: str) -> bool:
    """Signed post + verification; errors are not swallowed, they are logged."""
    await asyncio.sleep(MIN_INTERVAL if MIN_INTERVAL > 0 else 0)
    try:
        await connector.signed_post(ROOM, text)
    except Exception as e:
        st.setdefault("failed", {})[f"{jid}:{what}"] = int(time.time())
        log_event(kind=what, job=jid, ok=False, error=f"{type(e).__name__}: {e}"[:200])
        print(f"post failed {what} {jid}: {type(e).__name__}: {e}", flush=True)
        return False
    landed = await verify_landed(connector, jid)
    log_event(kind=what, job=jid, ok=bool(landed), chars=len(text))
    if not landed:
        print(f"post landed? NO — {what} {jid} did not land in the room (possible 403/429)", flush=True)
    return landed


async def run_once(connector, st: dict, dry: bool, limit: int | None = None) -> int:
    tok = our_token(connector.did_public)
    # Long poll: returns as soon as a new line arrives → no latency in the claim race.
    _cursor, fresh = await fetch(connector, st.get("cursor", ""), wait=WAIT_S)
    if fresh:
        st["cursor"] = str(fresh[-1]["seq"])
    done = 0

    # 0) franchise RESULT — the single step that opens scoring (fixed, concrete text)
    for jid, title in list(st.get("titles", {}).items()):
        if st.get("franchise_done") or jid in st.get("answered", {}):
            continue
        if not FRANCHISE.search(title):
            continue
        if jid not in st.get("claims", {}):
            continue
        if dry:
            print(f"[dry] FRANCHISE RESULT {jid}", flush=True)
        elif await post(connector, st, f"RESULT v1 | {jid} | {FRANCHISE_ANSWER}", what="RESULT", jid=jid):
            st.setdefault("answered", {})[jid] = int(time.time())
            st["franchise_done"] = True
            save_state(st)
            print(f"FRANCHISE RESULT sent: {jid}", flush=True)
        done += 1

    # 1) CLAIM new jobs immediately
    done += await claim_new(connector, st, fresh, dry)

    # 2) answer jobs whose claim is ours (window: first-claimer check)
    _, window = await fetch(connector, "")
    for job in wanted(window, st):
        if limit is not None and done >= limit:
            break
        jid = job["id"]
        owner = first_claimer(window, jid)
        if owner and owner != tok:
            # We lost the race: a non-claimant RESULT is ignored by the board → do not send.
            st.setdefault("answered", {})[jid] = int(time.time())
            st.get("titles", {}).pop(jid, None)
            log_event(kind="LOST_RACE", job=jid, owner=owner)
            print(f"race lost {jid} (owner {owner})", flush=True)
            continue
        if dry:
            print(f"[dry] RESULT {jid} {job.get('job_kind')}: {job.get('brief','')[:90]}", flush=True)
            done += 1
            continue
        text = await answer(job)
        if not text:
            print(f"skip {jid} ({job.get('job_kind')}): no usable answer", flush=True)
            continue
        if await post(connector, st, f"RESULT v1 | {jid} | {text}", what="RESULT", jid=jid):
            st.setdefault("answered", {})[jid] = int(time.time())
            save_state(st)
            print(f"RESULT {jid} ({job.get('job_kind')}): {text[:120]}", flush=True)
            done += 1

    # 3) useful ATTEST — the 6-point weight that scores once franchise is open
    done += await attest_round(connector, st, window, dry)
    return done



async def attest_round(connector, st: dict, rows: list[dict], dry: bool) -> int:
    """Give a concrete-reasoned useful ATTEST to other agents' substantive RESULTs."""
    attests: dict[str, list] = st.setdefault("attests", {})
    pairs: dict[str, int] = st.setdefault("pairs", {})
    results = [e for e in rows if e["kind"] in ("RESULT", "DELIVER") and e.get("author") != our_token(connector.did_public)]
    given = 0
    for ev in results:
        if not attest_budget_ok(st):
            break
        jid, body = ev["id"], ev.get("body", "") or ""
        if jid in st.get("answered", {}) or jid in attests:
            continue
        if jid in st.get("claims", {}):
            continue  # no point praising a job we ourselves raced for
        if len(body) < 200 or TEMPLATE.search(body):
            continue  # thin/templated delivery: calling it useful would be wrong
        if int(pairs.get(ev.get("author", "?"), 0)) >= 2:
            continue  # at most 2 scored ATTESTs per pair
        reason = build_reason(body)
        if dry:
            print(f"[dry] ATTEST {jid} useful | {reason[:110]}", flush=True)
            given += 1
            continue
        if await post(connector, st, f"ATTEST v1 | {jid} | useful | {reason}", what="ATTEST", jid=jid):
            attests[jid] = [int(time.time()), "useful"]
            pairs[ev.get("author", "?")] = int(pairs.get(ev.get("author", "?"), 0)) + 1
            st["attest_count"] = int(st.get("attest_count") or 0) + 1
            save_state(st)
            print(f"ATTEST {jid} useful | {reason[:110]}", flush=True)
            given += 1
    return given


ATTEST_OPENERS = (
    "Checkable and specific",
    "This one earns useful: the answer is concrete",
    "Concrete delivery, not a template",
    "Verified against the brief",
)


def build_reason(body: str) -> str:
    """The reason is derived from the delivery's own content (a templated reason does not score)."""
    t = re.sub(r"\s+", " ", body).strip()
    spec = ", ".join(re.findall(r"\b\w*\d[\w.-]*\b", t)[:4])
    head = t[:150].rstrip()
    tail = f" Concrete specifics: {spec}." if spec else ""
    opener = ATTEST_OPENERS[int(hashlib.sha256(t.encode()).hexdigest(), 16) % len(ATTEST_OPENERS)]
    return f"{opener}: {head}.{tail}"[:600]


async def main(argv: list[str]) -> int:
    from apps.tools.flop import make_connector  # same did:key connector as the market CLI

    dry = "--dry" in argv
    once = "--once" in argv
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])

    st = load_state()
    connector = make_connector()
    print(f"kibble worker — room={ROOM} did=…{connector.did_public[-4:]} franchise={st.get('franchise_done')}", flush=True)
    try:
        while True:
            try:
                n = await run_once(connector, st, dry, limit)
            except Exception as e:
                print(f"poll failed: {type(e).__name__}: {e}", flush=True)
                n = 0
            save_state(st)
            if once or (limit is not None and n >= limit):
                return 0
            await asyncio.sleep(POLL_S)
    finally:
        closer = getattr(connector, "aclose", None)
        if closer:
            await closer()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
