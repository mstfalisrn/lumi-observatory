"""Producer tests — the agent's ability to actually finish a production brief.

The market's briefs are one-line titles (\"craftreel vertical-reel\"); the LLM is
what turns one into a usable deliverable. These tests pin the honest contract:
a fake deliverable is never returned, and a mock model produces nothing at all.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "scheduler"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages"))

from tclk_producer import MAX_CHARS, build_messages, clean, kind_of, produce, usable

from connectors.tclk import claim_seed, derived_hashlock


class _Result:
    def __init__(self, text):
        self.text = text


class _Provider:
    """Minimal stand-in for the shared LLM client."""

    name = "test"

    def __init__(self, text="", boom=False):
        self.text = text
        self.boom = boom
        self.seen = []

    async def chat(self, messages, **kw):
        self.seen.append(messages)
        if self.boom:
            raise RuntimeError("provider down")
        return _Result(self.text)


class _MockProvider(_Provider):
    name = "mock"


def test_kind_of_maps_market_titles():
    assert kind_of("craftreel vertical-reel")[0] == "vertical video script"
    assert kind_of("ipfspost post-or-caption")[0] == "social post"
    assert kind_of("gardenprompt prompt-pack")[0] == "prompt pack"
    assert kind_of("glassreport written-report")[0] == "short report"
    assert kind_of("unityaudit checklist-audit")[0] == "short report"
    assert kind_of("realtylisting catalog-listing")[0] == "listing copy"
    assert kind_of("kitchenthumbnail thumbnail-image")[0] == "art direction brief"
    assert kind_of("mystery-7169 something-else")[0] == "generic"


def test_clean_strips_fences_and_caps_length():
    assert clean("```markdown\nHook line\nBody\n```") == "Hook line\nBody"
    long_text = ("Sentence about the job. " * 200).strip()
    out = clean(long_text)
    assert len(out) <= MAX_CHARS
    assert out.endswith(".")


def test_usable_rejects_mock_and_refusals():
    assert usable("A real deliverable line with enough words in it.")
    assert not usable("[mock] plan: gather context")
    assert not usable("Sorry, I cannot help with that request right now.")
    assert not usable("short")


def test_produce_returns_the_deliverable_and_passes_the_brief():
    provider = _Provider(
        "Hook: your kitchen deserves better light.\nBody line one.\nCTA: book today. #light #home"
    )
    out = asyncio.run(produce("kitchenthumbnail thumbnail-image", "make it warm", provider=provider))
    assert out and out.startswith("Hook:")
    sent = provider.seen[0]
    assert "kitchenthumbnail thumbnail-image" in sent[1].content
    assert "make it warm" in sent[1].content


def test_produce_never_fakes_work():
    assert asyncio.run(produce("craftreel vertical-reel", provider=_MockProvider("real text here"))) is None
    assert asyncio.run(produce("craftreel vertical-reel", provider=_Provider(boom=True))) is None
    assert asyncio.run(produce("craftreel vertical-reel", provider=_Provider(""))) is None
    assert asyncio.run(produce("", provider=_Provider("anything at all here"))) is None
    assert (
        asyncio.run(
            produce("craftreel vertical-reel", provider=_Provider("Sorry, I cannot help with that."))
        )
        is None
    )


def test_build_messages_carries_the_kv_note_as_task_detail():
    msgs = build_messages("gardenprompt prompt-pack", "Write prompts for a balcony herb garden.")
    assert msgs[0].role == "system"
    assert "task detail" in msgs[1].content
    assert "balcony herb garden" in msgs[1].content


def test_derived_claim_secret_is_recomputable_and_publicly_checkable():
    seed = claim_seed(bytes(range(32)))
    preimage, statement = derived_hashlock("0x" + "ab" * 32, seed)

    # same offer + same key -> same secret (a lock landing days later still claims)
    assert derived_hashlock("0x" + "ab" * 32, seed) == (preimage, statement)
    # a different offer never reuses the secret
    assert derived_hashlock("0x" + "cd" * 32, seed)[0] != preimage
    # the statement is sha256 of the preimage — anyone can verify, only we reveal
    import hashlib

    assert statement == "0x" + hashlib.sha256(bytes.fromhex(preimage[2:])).hexdigest()
    # the signing key itself is never the HMAC key
    assert claim_seed(bytes(range(32))) != bytes(range(32))
