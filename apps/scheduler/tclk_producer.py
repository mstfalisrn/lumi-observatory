"""tclk/1 work producer — the agent's ability to actually do the job.

Marketplace briefs are production jobs: a caption, a reel script, a prompt pack,
a short report, a checklist. None of them is deterministic, so the deterministic
solver honestly returns None and every deal dies as `no_answer`. This module
turns such a brief into a concrete deliverable with the configured LLM, and
returns None when the model is unreachable — work is never faked.

Contract with the caller:
  * input   — the brief text (plus, when the offer pointed at one, the /kv note)
  * output  — the deliverable itself, plain text, <= MAX_CHARS, or None
  * no side effects: posting the deliverable stays in the scheduler (build_delivery).
"""

from __future__ import annotations

import asyncio
import re

MAX_CHARS = 1200
MIN_CHARS = 24
TIMEOUT_S = 150.0
# Output budget must leave room for a REASONING model's hidden tokens: with
# reasoning_effort=xhigh a 700-token cap is spent before any answer text is
# emitted and the call comes back empty, which reads as "no answer".
MAX_TOKENS = 6000

_SYSTEM = (
    "You are LUMI, an autonomous delivery agent on the tclk/1 work marketplace. "
    "A client posts a one-line brief; you return the finished deliverable. "
    "Rules: output ONLY the deliverable as plain text; no preamble, no apologies, "
    "no markdown code fences; be concrete and specific; "
    f"stay under {MAX_CHARS} characters. Write in the language of the brief."
)

# Brief → the shape of the answer. The market titles jobs as "<slug> <task-class>",
# so the class in the title is enough to pick a deliverable format the payer can use.
_CLASSES: tuple[tuple[str, str, str], ...] = (
    (
        r"caption|post|social",
        "social post",
        "Write the finished post: a scroll-stopping first line, 2-3 short body lines, "
        "one call to action, then 4 relevant hashtags.",
    ),
    (
        r"reel|short|video|clip|tiktok",
        "vertical video script",
        "Write a shot list for a vertical short: 4-6 numbered shots, each with "
        "on-screen text and the spoken line, plus a hook in the first 2 seconds.",
    ),
    (
        r"prompt",
        "prompt pack",
        "Write 6 ready-to-paste prompts, numbered, each one line long and specific "
        "enough to run as-is, covering the brief's use case.",
    ),
    (
        r"report|audit|checklist|review",
        "short report",
        "Write a compact report: 3-5 findings as bullets, each with the evidence and "
        "the concrete action it implies, then a one-line verdict.",
    ),
    (
        r"script|transcript|dialog",
        "dialogue script",
        "Write the script as labelled lines (SPEAKER: line), 8-14 lines, with a clear "
        "opening, one turn and a closing line.",
    ),
    (
        r"thumbnail|banner|image|cover|mockup|ui",
        "art direction brief",
        "Write an art-direction brief the designer can execute: subject and framing, "
        "exact on-image text, palette with hex codes, and the aspect ratio.",
    ),
    (
        r"course|lesson|module|training",
        "mini-course outline",
        "Write the outline: 3-4 modules with a one-line outcome each, and for module 1 "
        "the concrete exercise.",
    ),
    (
        r"listing|catalog|product|offer",
        "listing copy",
        "Write the listing: a title under 70 characters, 5 benefit bullets, and the "
        "spec block as key: value lines.",
    ),
    (
        r"story|frame|storyboard",
        "storyboard",
        "Write the storyboard: 4-6 numbered frames, each with the visual, the "
        "on-screen text and the narration line.",
    ),
    (
        r"live|vod|cut|edit",
        "clip edit plan",
        "Write the edit plan as a timecoded list (00:00-00:12 ...): what is on "
        "screen, the caption, and the reason that moment earns its place.",
    ),
    (
        r"spec|harness|unit|task",
        "task specification",
        "Write the spec: goal in one line, then the acceptance checks as a "
        "checklist a reviewer can run, then the interfaces touched.",
    ),
    (
        r"email|newsletter|outreach",
        "email draft",
        "Write the email: a subject line, a 3-sentence body, and one clear ask.",
    ),
)


def kind_of(brief: str) -> tuple[str, str]:
    """Classify a brief into a deliverable shape. Returns (kind, instruction)."""
    b = (brief or "").lower()
    for pattern, kind, instruction in _CLASSES:
        if re.search(pattern, b):
            return kind, instruction
    return (
        "generic",
        "Produce exactly the artifact the brief asks for, complete and ready to use.",
    )


def _llm():
    """Import the shared LLM client either as a package or as a top-level module."""
    try:
        from agent_core import llm
    except ImportError:
        from packages.agent_core import llm
    return llm


def build_messages(brief: str, note: str = "") -> list[object]:
    """System + user prompt for one brief. The note (if any) is the full task text."""
    LLMMessage = _llm().LLMMessage

    _kind, instruction = kind_of(brief)
    task = brief.strip()
    if note.strip() and note.strip() not in task:
        task = f"{task}\n\n--- task detail ---\n{note.strip()[:4000]}"
    user = f"Brief: {task}\n\nDeliverable to return: {instruction}"
    return [LLMMessage("system", _SYSTEM), LLMMessage("user", user)]


def clean(text: str, max_chars: int = MAX_CHARS) -> str:
    """Fold a model reply into one shippable plain-text deliverable."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    lines = [ln.rstrip() for ln in t.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        best = -1
        for sep in ("\n", ". ", "! ", "? "):
            i = cut.rfind(sep)
            best = max(best, i)
        if best > max_chars // 3:
            cut = cut[: best + (1 if cut[best] in ".!?" else 0)]
        else:
            i = cut.rfind(" ")
            if i > 0:
                cut = cut[:i]
        t = cut.strip()
    return t


# Two or more distinct <placeholder> tokens means the model shipped the task's
# skeleton instead of the work ("<room>|<nonce>|<text>"); that is not a delivery.
_PLACEHOLDERS = re.compile(r"<([a-z_][a-z0-9_ -]{1,24})>")


def placeholder_stub(text: str) -> bool:
    """True when the reply is still a template, not a finished deliverable."""
    found = {m.group(1).strip().lower() for m in _PLACEHOLDERS.finditer(text or "")}
    return len(found) >= 2


def usable(text: str) -> bool:
    """Is this a real deliverable (not a mock, a refusal or an empty shell)?"""
    t = (text or "").strip()
    if len(t) < MIN_CHARS or len(t.split()) < 4:
        return False
    if placeholder_stub(t):
        return False
    return not t.lower().startswith(("[mock]", "sorry", "i can't", "i cannot"))


async def produce(
    brief: str,
    note: str = "",
    *,
    provider=None,
    max_chars: int = MAX_CHARS,
) -> str | None:
    """Do the job: brief in, finished deliverable out (None if we could not do it)."""
    if not (brief or "").strip():
        return None
    if provider is None:
        provider = _llm().build_provider()
    # The mock provider answers every prompt with a plan — shipping that as the
    # deliverable would be a lie, so an unconfigured model means "not done".
    if getattr(provider, "name", "") == "mock":
        return None
    messages = build_messages(brief, note)
    try:
        result = await asyncio.wait_for(
            provider.chat(messages, max_tokens=MAX_TOKENS, purpose="tclk-produce"),
            TIMEOUT_S,
        )
    except Exception:
        return None
    text = clean(getattr(result, "text", "") or "", max_chars)
    return text if usable(text) else None
