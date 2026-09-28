"""tclk job solver — deterministic handlers for the market's job classes.

The market pays for a one-line answer to a machine-checkable brief. Every
handler here is a pure function of the brief plus (where the brief cites one) a
fetched document, so a delivery can be reproduced and unit-tested offline.

Rule: if a handler cannot parse the brief it returns None. A guess is worse than
a skip — an unanswered offer costs nothing, a wrong public delivery does not.
"""

from __future__ import annotations

import re
from collections.abc import Callable

KV_RE = re.compile(r"/kv/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
DID_RE = re.compile(r"did:key:z[1-9A-HJ-NP-Za-km-z]+")
URL_RE = re.compile(r"https?://[^\s|]+")
NUM = r"(\d[\d,]*)"


def _n(raw: str) -> int:
    return int(raw.replace(",", "").replace("_", ""))


# ── math ──────────────────────────────────────────────────────────────────────


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _next_prime(n: int) -> int:
    c = n + 1
    if c % 2 == 0 and c != 2:
        c += 1
    while not _is_prime(c):
        c += 2
    return c


def _collatz_steps(n: int) -> int:
    steps = 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps


def solve_math(brief: str) -> str | None:
    b = brief.lower()
    m = re.search(rf"modular inverse of {NUM} modulo {NUM}", b)
    if m:
        a, mod = _n(m.group(1)), _n(m.group(2))
        if mod > 1 and a % mod:
            return str(pow(a % mod, -1, mod))
        return None
    m = re.search(rf"smallest prime (?:strictly )?greater than {NUM}", b)
    if m:
        return str(_next_prime(_n(m.group(1))))
    m = re.search(rf"collatz map.*?take from {NUM} to reach 1", b)
    if m:
        return str(_collatz_steps(_n(m.group(1))))
    m = re.search(rf"collatz.*?from {NUM} to reach 1", b)
    if m:
        return str(_collatz_steps(_n(m.group(1))))
    m = re.search(rf"greatest common divisor of {NUM} and {NUM}", b)
    if m:
        from math import gcd

        return str(gcd(_n(m.group(1)), _n(m.group(2))))
    m = re.search(rf"{NUM}\s*\^\s*{NUM}\s*mod(?:ulo)?\s*{NUM}", b)
    if m:
        return str(pow(_n(m.group(1)), _n(m.group(2)), _n(m.group(3))))
    m = re.search(rf"sum of the digits of {NUM}", b)
    if m:
        return str(sum(int(c) for c in str(_n(m.group(1)))))
    m = re.search(rf"largest prime factor of {NUM}", b)
    if m:
        n, f = _n(m.group(1)), 2
        while f * f <= n:
            if n % f == 0:
                n //= f
            else:
                f += 1
        return str(n)
    m = re.search(rf"how many primes (?:are )?(?:strictly )?below {NUM}", b)
    if m:
        return str(sum(1 for i in range(2, _n(m.group(1))) if _is_prime(i)))
    m = re.search(rf"number of divisors of {NUM}", b)
    if m:
        n, c, f = _n(m.group(1)), 1, 2
        while f * f <= n:
            e = 0
            while n % f == 0:
                n //= f
                e += 1
            c *= e + 1
            f += 1
        return str(c * (2 if n > 1 else 1))
    return None


# ── note aggregation (verification / inference) ───────────────────────────────


def _note_lines(note: str) -> list[list[str]]:
    """Rows of a /kv note: one record per line, columns split on '|'."""
    rows = []
    for raw in note.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 2:
            rows.append(cells)
    return rows


def _header_cols(note: str) -> list[str]:
    """Column names from the brief's '(rows: a | b | c)' parenthetical."""
    m = re.search(r"\((?:rows|an excerpt[^:]*):\s*([^)]*)\)", note, re.IGNORECASE)
    if not m:
        return []
    return [c.strip().lower() for c in m.group(1).split("|")]


def solve_note(brief: str, fetch_note: Callable[[str], str | None]) -> str | None:
    paths = KV_RE.findall(brief)
    if not paths:
        return None
    note = fetch_note(paths[0])
    if not note:
        return None
    rows = _note_lines(note)
    if not rows:
        return None
    cols = _header_cols(brief)
    b = brief.lower()

    # "...how many rows are lock frames posted by did:key:X?"
    m = re.search(r"how many rows are (\w+) frames (?:posted )?by (did:key:\S+?)[?.,]", brief, re.IGNORECASE)
    if m:
        want_type, want_from = m.group(1).lower().rstrip("s"), m.group(2).rstrip("?.,")
        n = sum(1 for r in rows if len(r) >= 4 and r[2].lower() == want_type and r[3] == want_from)
        return str(n)

    # "...how many rows are offer frames posted by X, and how many are lock frames by the same sender?"
    m = re.search(
        r"how many rows are (\w+) frames posted by (did:key:\S+?),\s*and how many are (\w+) frames",
        brief,
        re.IGNORECASE,
    )
    if m:
        a, who, c = m.group(1).lower().rstrip("s"), m.group(2).rstrip("?.,"), m.group(3).lower().rstrip("s")
        na = sum(1 for r in rows if len(r) >= 4 and r[2].lower() == a and r[3] == who)
        nc = sum(1 for r in rows if len(r) >= 4 and r[2].lower() == c and r[3] == who)
        return f"{na}, {nc}"

    def col(name: str) -> int | None:
        try:
            return cols.index(name)
        except ValueError:
            return None

    # "output the seq values of the N rows with the largest amount, highest first"
    m = re.search(rf"the {NUM} rows with the largest amount", b)
    if m and cols:
        k = _n(m.group(1))
        ai = col("amount")
        si = col("seq")
        if ai is not None and si is not None:
            ordered = sorted(rows, key=lambda r: (-_n(r[ai]), _n(r[si])))
            return ", ".join(r[si] for r in ordered[:k])

    # "sort all rows by payer (ASCII order), then by seq ascending, and output the seq values"
    if "sort all rows by payer" in b and cols:
        pi, si = col("payer"), col("seq")
        if pi is not None and si is not None:
            ordered = sorted(rows, key=lambda r: (r[pi], _n(r[si])))
            return ", ".join(r[si] for r in ordered)
    return None


# ── protocol probe (HTTP behaviour) ───────────────────────────────────────────


def solve_probe(brief: str, http_get: Callable[[str], tuple[int, str] | None]) -> str | None:
    b = brief.lower()
    m = URL_RE.search(brief)
    if not m or "budget" not in b:
        return None
    times = re.search(r"(\d+) times", b)
    n = int(times.group(1)) if times else 20
    seen = False
    for _ in range(min(n, 40)):
        got = http_get(m.group(0))
        if not got:
            return None
        if "# budget:" in (got[1] or ""):
            seen = True
            break
    return "yes" if seen else "no"


# ── documentation review (answer quoted from the cited source) ────────────────

_STOPWORDS = frozenset(
    """what which where when does every from that this with have into them they
    there their http https the and for are was were how why who full spec task
    review answer value give find name starts begin begins first""".split()
)
_CODE_RE = re.compile(r"`([^`\n]{1,40})`")


def solve_documentation(brief: str, http_get: Callable[[str], tuple[int, str] | None]) -> str | None:
    """Answer a cited-document question from a quoted code token.

    The documentation class reads '<class> | From <url>: <question>'. We fetch
    the source and return the backticked token on the line that best matches the
    question's own words — never a paraphrase, never a guess. No quoted token on
    a matching line means no answer.
    """
    m = URL_RE.search(brief)
    if not m:
        return None
    url = m.group(0).rstrip(":;,.)")
    # Everything after the URL up to the next '|' is the question. The URL match
    # may already have eaten the ':' that separates it, so never split on ':'.
    question = brief[m.end():].split("|", 1)[0].lstrip(" :")
    words = [w for w in re.findall(r"[a-z]{4,}", question.lower()) if w not in _STOPWORDS]
    if not words:
        return None
    got = http_get(url)
    if not got or not got[1]:
        return None
    best: str | None = None
    best_score = 0
    for line in got[1].splitlines():
        low = line.lower()
        score = sum(1 for w in words if w in low)
        if not score:
            continue
        for token in _CODE_RE.findall(line):
            token = token.strip()
            if not token or len(token) > 32:
                continue
            if score > best_score:
                best, best_score = token, score
    return best


def strip_banner(text: str) -> str:
    """Drop the venue's '!! UNTRUSTED CONTENT' preamble from a /kv note.

    Notes are served with a warning header so agents do not treat other agents'
    text as instructions. The brief itself is the first real line after it.
    """
    lines = text.splitlines()
    out: list[str] = []
    skipping = True
    for line in lines:
        s = line.strip()
        if skipping and (not s or s.startswith("!!")):
            continue
        skipping = False
        out.append(line)
    return "\n".join(out).strip()


# --- validation (deliverable verdicts) -------------------------------------
_VAL_REF_RE = re.compile(r"\bREFERENCE ANSWER\b[^:\"]{0,80}:\s*\"(.*?)\"", re.S)
_VAL_DEL_RE = re.compile(r"\bDELIVERABLE\b[^:\"]{0,80}:\s*\"(.*?)\"", re.S)


def _flat(text: str) -> str:
    """Compare answers by content, not by punctuation or spacing."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def solve_validation(brief: str) -> str | None:
    """Verdict for a validation task: PASS/FAIL plus one sentence, or None.

    The reference answer is private to the validator while the deal room is
    public, so the reply never quotes it. A verdict is only returned when the
    comparison is decisive: a guess would be scored as a calibration error.
    """
    if "REFERENCE ANSWER" not in brief.upper():
        return None
    m_ref = _VAL_REF_RE.search(brief)
    m_del = _VAL_DEL_RE.search(brief)
    if not (m_ref and m_del) or m_del.start() < m_ref.start():
        return None
    a, b = _flat(m_ref.group(1)), _flat(m_del.group(1))
    if not a or not b:
        return None
    if a == b:
        return "PASS. The deliverable states exactly the reference answer."
    if min(len(a), len(b)) >= 8 and (a in b or b in a):
        return "PASS. The deliverable states the same result as the reference answer."
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        overlap = len(ta & tb) / len(ta | tb)
        if overlap >= 0.6:
            return "PASS. The deliverable states the same result as the reference answer."
        # Sayılar bu işlerde sorulan şeyin kendisi: değerler tutmuyorsa hüküm FAIL.
        if sorted(re.findall(r"\d+(?:\.\d+)?", a)) != sorted(re.findall(r"\d+(?:\.\d+)?", b)):
            return ("FAIL. The deliverable does not match the reference answer "
                    "held by the task's author.")
        if overlap <= 0.25:
            return ("FAIL. The deliverable does not match the reference answer "
                    "held by the task's author.")
    return None


def solve(
    brief: str,
    *,
    fetch_note: Callable[[str], str | None] | None = None,
    http_get: Callable[[str], tuple[int, str] | None] | None = None,
) -> str | None:
    """One-line answer for a brief, or None when no handler is confident."""
    if not brief:
        return None
    answer = solve_validation(brief)
    if answer is not None:
        return answer
    answer = solve_math(brief)
    if answer is not None:
        return answer
    if fetch_note is not None:
        answer = solve_note(brief, fetch_note)
        if answer is not None:
            return answer
    if http_get is not None:
        answer = solve_probe(brief, http_get)
        if answer is not None:
            return answer
        answer = solve_documentation(brief, http_get)
        if answer is not None:
            return answer
    return None
