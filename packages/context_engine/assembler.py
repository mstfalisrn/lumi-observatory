# LUMI — Context Assembler / Inspector
# Layers context by token budget; does NOT expose hidden chain-of-thought.
# Metadata for each segment: segment_type, source_id, title, token_count, relevance,
# freshness, confidence, included_reason, contains_untrusted, redaction_count.
# Phase 4: layers are disjoint (system_policy / task_goal / memory / tool_schemas / untrusted),
#       overwrite bug fixed (dict -> list), token counting improved.

from __future__ import annotations

import dataclasses
import re
import time
from typing import Any


@dataclasses.dataclass
class ContextSegment:
    segment_type: str
    source_id: str
    title: str
    content: str
    token_count: int = 0
    relevance_score: float = 0.0
    freshness: str = ""
    confidence: float = 0.0
    included_reason: str = ""
    contains_untrusted_input: bool = False
    redaction_count: int = 0


# Simple token estimate (~4 chars/token) — count more accurately if tiktoken is available
def estimate_tokens(text: str) -> int:
    # Use tiktoken's cl100k_base when available; otherwise use the fallback
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return max(1, len(enc.encode(text)))
    except Exception:
        return max(1, len(text) // 4)


# Always reserved output share (output reserve cannot be zeroed)
OUTPUT_RESERVE_TOKENS = 2048

# Layer definition — 5 main layers + subtypes for compatibility
# Priority order: lower number = higher priority (included first)
LAYER_PRIORITY: dict[str, int] = {
    "system_policy": 0,
    "task_goal": 1,
    "conversation_window": 2,
    # memory group — all in the same layer
    "episodic_memory": 3,
    "semantic_memory": 3,
    "procedural_memory": 3,
    "memory": 3,
    "tool_schemas": 4,
    # untrusted — lowest priority, separately limited
    "untrusted": 5,
    "tool_output": 5,
    "external_data": 5,
}

# Canonical layer order (SEQUENCE) — old + new types for backward compatibility
SEQUENCE = [
    "system_policy",
    "task_goal",
    "conversation_window",
    "episodic_memory",
    "semantic_memory",
    "procedural_memory",
    "memory",
    "tool_schemas",
    "untrusted",
]

# Layer -> maximum share (budget ratio) — untrusted is limited
LAYER_BUDGET_RATIO: dict[str, float] = {
    "untrusted": 0.25,  # untrusted cannot exceed 25% of total budget
}

# Redaction placeholders — for broad counting
_REDACTED_RE = re.compile(r"<[^>]*REDACTED[^>]*>")

# Boundary markers to isolate untrusted content
UNTRUSTED_BEGIN = "<<<UNTRUSTED_DATA_BEGIN>>>"
UNTRUSTED_END = "<<<UNTRUSTED_DATA_END>>>"


def _layer_of(segment_type: str) -> str:
    """Map segment type to canonical layer."""
    if segment_type in LAYER_PRIORITY:
        # don't normalize memory subtypes to 'memory' — priority is the same
        return segment_type
    # unknown types are not counted as 'untrusted'; they get their own layer
    return segment_type


def _priority(segment_type: str) -> int:
    return LAYER_PRIORITY.get(segment_type, 99)


class ContextAssembler:
    """Context layer aggregator; respects token budget, isolates untrusted."""

    # public constants
    SEQUENCE = SEQUENCE
    OUTPUT_RESERVE_TOKENS = OUTPUT_RESERVE_TOKENS

    def __init__(self, max_tokens: int = 60000, *, redactor: Any = None) -> None:
        self.max_tokens = max(OUTPUT_RESERVE_TOKENS + 1024, max_tokens)
        self.redactor = redactor
        # Phase 4 fix: dict -> list (overwrite bug resolution)
        # The same segment_type can be added multiple times (e.g. memory multiple hits)
        self._segments: list[ContextSegment] = []

    def add(self, segment_type: str, content: str, *, title: str = "", source_id: str = "",
            relevance: float = 0.0, confidence: float = 0.0, untrusted: bool = False) -> None:
        # DLP: redact
        if self.redactor is not None:
            scrub = self.redactor.scrub
        else:
            from observability.security import redact
            scrub = redact  # type: ignore[assignment]
        content = scrub(content)

        # Untrusted isolation: separate marking + boundary
        is_untrusted = untrusted or segment_type in ("untrusted", "tool_output", "external_data")
        if is_untrusted:
            # wrap content inside boundary (prompt injection mitigation)
            if UNTRUSTED_BEGIN not in content:
                content = f"{UNTRUSTED_BEGIN}\n{content}\n{UNTRUSTED_END}"

        # redaction counting — count all variants
        redactions = len(_REDACTED_RE.findall(content))

        seg = ContextSegment(
            segment_type=segment_type,
            source_id=source_id,
            title=title,
            content=content,
            token_count=estimate_tokens(content),
            relevance_score=relevance,
            freshness=str(int(time.time())),
            confidence=confidence,
            included_reason=f"layer {segment_type} -> budget-rule selection",
            contains_untrusted_input=is_untrusted,
            redaction_count=redactions,
        )
        # No overwrite — append
        self._segments.append(seg)

    def assemble(self) -> tuple[list[ContextSegment], str]:
        """Returns segments in order within budget + produces a combined auditable prompt."""
        ordered: list[ContextSegment] = []
        budget = self.max_tokens - OUTPUT_RESERVE_TOKENS
        used = 0
        untrusted_used = 0
        untrusted_budget = int(budget * LAYER_BUDGET_RATIO.get("untrusted", 0.25))

        # Priority order fixed, then relevance ordering
        # Process types in Sequence by priority order; multiple segments of the same type are possible
        # First sequence order
        seq_segments: list[ContextSegment] = []
        remaining: list[ContextSegment] = []
        # group by segment_type but preserve order
        seg_by_type: dict[str, list[ContextSegment]] = {}
        for s in self._segments:
            seg_by_type.setdefault(s.segment_type, []).append(s)
        # add by SEQUENCE order
        for stype in self.SEQUENCE:
            for seg in seg_by_type.pop(stype, []):
                seq_segments.append(seg)
        # Remaining unknown types
        for segs in seg_by_type.values():
            remaining.extend(segs)
        # sort by remaining relevance
        remaining.sort(key=lambda s: s.relevance_score, reverse=True)

        all_in_order = seq_segments + remaining

        for seg in all_in_order:
            # layer budget check (untrusted limited)
            if seg.contains_untrusted_input:
                if untrusted_used + seg.token_count > untrusted_budget:
                    seg.included_reason = "untrusted layer budget exceeded → skipped"
                    continue
            # overall budget check — system_policy and task_goal always included (critical)
            if used + seg.token_count > budget and seg.segment_type not in ("system_policy", "task_goal"):
                seg.included_reason = "token budget exceeded → skipped"
                continue
            ordered.append(seg)
            used += seg.token_count
            if seg.contains_untrusted_input:
                untrusted_used += seg.token_count

        # auditable prompt: info note per segment, no hidden thoughts
        # Layer boundaries are explicitly marked
        parts = []
        for seg in ordered:
            untagged = " [UNTRUSTED]" if seg.contains_untrusted_input else ""
            parts.append(
                f"### {seg.segment_type}{untagged} ({seg.included_reason})\n{seg.content}"
            )
        prompt = "\n\n".join(parts)
        return ordered, prompt

    def inspector_metadata(self) -> list[dict]:
        # Phase 4: return all segments (excluded ones also with reason)
        return [
            {
                "segment_type": s.segment_type,
                "title": s.title,
                "token_count": s.token_count,
                "relevance_score": s.relevance_score,
                "freshness": s.freshness,
                "confidence": s.confidence,
                "included_reason": s.included_reason,
                "contains_untrusted_input": s.contains_untrusted_input,
                "redaction_count": s.redaction_count,
            }
            for s in self._segments
        ]

    # Helper: layer-based token totals (for inspector)
    def layer_token_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self._segments:
            counts[s.segment_type] = counts.get(s.segment_type, 0) + s.token_count
        return counts
