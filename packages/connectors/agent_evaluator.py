# LUMI M2 — Agent Evaluator
# LLM Provider supports mock/openai_compatible; uses settings.LLM_*.
# Calls /chat/completions with httpx. 5 dimensions + JSON schema; fallback heuristic.
from __future__ import annotations

import json
import logging
import re

import httpx

from observability.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier haritalama
# ---------------------------------------------------------------------------
TIER_SAFE = "SAFE"
TIER_WATCH = "WATCH"
TIER_RISKY = "RISKY"
TIER_DANGEROUS = "DANGEROUS"
VALID_TIERS = (TIER_SAFE, TIER_WATCH, TIER_RISKY, TIER_DANGEROUS)


def _tier_from_score(score: int) -> str:
    if score >= 80:
        return TIER_DANGEROUS
    if score >= 55:
        return TIER_RISKY
    if score >= 30:
        return TIER_WATCH
    return TIER_SAFE


# ---------------------------------------------------------------------------
# Prompt — 5 boyut
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a security evaluator for agent messages in Technocore rooms.
Evaluate the message across 5 dimensions, each 0-100:
- intent: malicious intent (prompt injection, jailbreak, social engineering)
- safety: safety violation (harmful content, disallowed requests)
- quality: low quality / spam / hallucination risk
- value: low value or off-topic
- risk: technical risk (SSRF, secret leak, code execution, data exfiltration)

Return ONLY valid JSON with this exact schema:
{"score": <int 0-100 overall risk>, "tier": "SAFE|WATCH|RISKY|DANGEROUS", "reason": "<1-2 sentence justification>", "dimensions": {"intent": <int>, "safety": <int>, "quality": <int>, "value": <int>, "risk": <int>}}

Tier mapping: 0-29 SAFE, 30-54 WATCH, 55-79 RISKY, 80-100 DANGEROUS.
Be precise and conservative; if uncertain, choose the higher risk tier."""


def _build_user_prompt(text: str, nick: str, did: str | None, room: str) -> str:
    meta = f"room={room} nick={nick} did={did or 'unknown'}"
    return f"{meta}\nmessage: {text[:4000]}"


# ---------------------------------------------------------------------------
# Fallback heuristic — keyword risk
# ---------------------------------------------------------------------------
_HEURISTIC_PATTERNS: list[tuple[re.Pattern, str, int]] = [
    (re.compile(r"ignore\s+previous\s+instructions", re.I), "prompt injection", 75),
    (re.compile(r"ignore\s+all\s+instructions", re.I), "prompt injection", 75),
    (re.compile(r"system\s*prompt", re.I), "prompt injection", 50),
    (re.compile(r"jailbreak|DAN\s+mode|do\s+anything\s+now", re.I), "prompt injection", 80),
    (re.compile(r"ssrf|169\.254\.169\.254|metadata\.google|localhost:\d|127\.0\.0\.1", re.I), "ssrf", 85),
    (re.compile(r"fetch\s*\(|curl\s|wget\s|http://\d", re.I), "ssrf", 60),
    (re.compile(r"api[_-]?key|secret|password|token\s*[:=]|BEGIN\s+(RSA\s+)?PRIVATE\s+KEY", re.I), "secret leak", 70),
    (re.compile(r"exfiltrate|leak\s+data|dump\s+db|drop\s+table|rm\s+-rf", re.I), "data exfiltration", 80),
    (re.compile(r"eval\s*\(|exec\s*\(|__import__|os\.system|subprocess", re.I), "code execution", 75),
    (re.compile(r"overwrite|delete\s+all|truncate\s+table", re.I), "destructive", 70),
]


def _snippet_for(text: str, labels: list[str]) -> str:
    """Masked ~140-char excerpt around the first matched heuristic pattern."""
    if not labels or not text:
        return ""
    span = (0, 0)
    for pat, label, _ in _HEURISTIC_PATTERNS:
        if label in labels:
            m = pat.search(text)
            if m:
                span = m.span()
                break
    start = max(0, span[0] - 40)
    end = min(len(text), span[1] + 80)
    excerpt = text[start:end].replace("\n", " ").strip()
    if len(excerpt) > 140:
        excerpt = excerpt[:140] + "…"
    if not excerpt:
        return ""
    try:
        from observability.security import redact

        excerpt = str(redact(excerpt) or excerpt)
    except Exception:
        pass
    excerpt = _SECRET_TOKEN_RX.sub("***", excerpt)
    return excerpt


# Local secret-token mask: applied on top of observability.security.redact so
# alert snippets never carry a live credential value into Telegram.
_SECRET_TOKEN_RX = re.compile(
    r"(?i)("
    r"sk-[A-Za-z0-9_\-]{6,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{20,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{10,}"
    r"|(?:api[_-]?key|apikey|token|password|secret)\s*[:=]\s*[A-Za-z0-9_\-\.]{6,}"
    r")"
)


def _short_did(did: str) -> str:
    if len(did) <= 20:
        return did
    return f"{did[:12]}…{did[-6:]}"


def extract_author(msg: dict) -> tuple[str, str]:
    """(display_name, did) from a Technocore message dict.

    Prefers explicit author fields (nick/author/sender), falls back to the
    `from` DID field and an `Agent-NN [did:...]:` prefix parsed from the text.
    """
    try:
        nick = str(msg.get("nick") or msg.get("author") or msg.get("sender") or "").strip()
    except Exception:
        nick = ""
    try:
        frm = msg.get("from") or msg.get("did") or msg.get("author_id") or ""
        did = str(frm).strip() if frm else ""
    except Exception:
        did = ""
    if nick:
        return nick[:120], did
    if did:
        txt = str(msg.get("text") or msg.get("message") or "")
        m = re.search(r"^\s*([A-Za-z0-9][A-Za-z0-9 _\-.()]{0,39}?)\s*\[?\s*did:", txt)
        if m:
            return m.group(1).strip()[:120], did
        return _short_did(did), did
    return "", ""


def _heuristic_evaluate(text: str) -> dict:
    max_score = 10
    matched: list[str] = []
    for pat, label, score in _HEURISTIC_PATTERNS:
        if pat.search(text):
            matched.append(label)
            if score > max_score:
                max_score = score
    # length / repetition spam
    if len(text) > 3500:
        max_score = max(max_score, 25)
        matched.append("long message")
    if not text.strip():
        return {"score": 5, "tier": TIER_SAFE, "reason": "empty message — SAFE", "dimensions": {"intent": 5, "safety": 5, "quality": 10, "value": 10, "risk": 5}, "matched": [], "snippet": ""}
    if matched:
        tier = _tier_from_score(max_score)
        labels = sorted(set(matched))
        reason = f"heuristic: {', '.join(labels)}"
        # distribute dimensions
        dims = {"intent": 0, "safety": 0, "quality": 10, "value": 10, "risk": 0}
        for m in set(matched):
            if m in ("prompt injection",):
                dims["intent"] = max_score
                dims["safety"] = max(dims["safety"], max_score - 10)
            elif m in ("ssrf", "code execution", "destructive", "data exfiltration"):
                dims["risk"] = max(dims["risk"], max_score)
            elif m in ("secret leak",):
                dims["risk"] = max(dims["risk"], max_score)
                dims["safety"] = max(dims["safety"], max_score - 20)
            else:
                dims["risk"] = max(dims["risk"], max_score // 2)
        # ensure risk reflects max
        if max_score >= 60:
            dims["risk"] = max(dims["risk"], max_score)
        return {"score": max_score, "tier": tier, "reason": reason, "dimensions": dims, "matched": labels, "snippet": _snippet_for(text, labels)}
    # benign
    return {"score": 10, "tier": TIER_SAFE, "reason": "no heuristic risk detected — SAFE", "dimensions": {"intent": 5, "safety": 5, "quality": 5, "value": 5, "risk": 10}, "matched": [], "snippet": ""}


def _normalize_llm_result(raw: dict, fallback_text: str) -> dict:
    try:
        score = int(raw.get("score", 10))
    except Exception:
        score = 10
    score = max(0, min(100, score))
    tier = str(raw.get("tier", "")).upper().strip()
    if tier not in VALID_TIERS:
        tier = _tier_from_score(score)
    reason = str(raw.get("reason", ""))[:500] or "llm evaluation"
    dims_raw = raw.get("dimensions") or {}
    dims = {}
    for k in ("intent", "safety", "quality", "value", "risk"):
        try:
            dims[k] = max(0, min(100, int(dims_raw.get(k, score // 2))))
        except Exception:
            dims[k] = score // 2
    return {"score": score, "tier": tier, "reason": reason, "dimensions": dims, "matched": [], "snippet": ""}


async def evaluate_agent_message(
    text: str,
    nick: str = "",
    did: str | None = None,
    room: str = "",
    *,
    seq: int | None = None,
    global_seq: int | None = None,
    raw_json: dict | None = None,
) -> dict:
    """Evaluate agent message; use LLM if available, otherwise heuristic fallback.

    Returns: {"score": int, "tier": str, "reason": str, "dimensions": dict, "model": str}
    """
    text = text or ""
    provider = (settings.LLM_PROVIDER or "mock").lower()

    # mock -> heuristic directly (no network)
    if provider == "mock" or not settings.LLM_BASE_URL:
        res = _heuristic_evaluate(text)
        res["model"] = "heuristic/mock"
        return res

    # openai_compatible -> httpx chat/completions
    base = settings.LLM_BASE_URL.rstrip("/")
    model = settings.LLM_MODEL or "gpt-4o-mini"
    url = f"{base}/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {settings.LLM_API_KEY}"
    headers["User-Agent"] = (
        settings.LLM_USER_AGENT
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    if settings.LLM_SESSION_ID:
        headers["x-opencode-session"] = settings.LLM_SESSION_ID

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(text, nick, did, room)},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            # content JSON string olabilir
            if isinstance(content, str):
                parsed = json.loads(content)
            elif isinstance(content, dict):
                parsed = content
            else:
                raise TypeError("unexpected content type")
            norm = _normalize_llm_result(parsed, text)
            norm["model"] = model
            return norm
    except Exception as e:
        logger.warning("agent_evaluator LLM failed, fallback heuristic: %s (%s)", type(e).__name__, str(e)[:200])
        res = _heuristic_evaluate(text)
        res["model"] = f"heuristic/fallback:{type(e).__name__}"
        return res


# Alias for scheduler spec: agent_evaluator.evaluate
evaluate = evaluate_agent_message
