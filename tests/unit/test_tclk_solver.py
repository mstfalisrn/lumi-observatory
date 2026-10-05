"""tclk solver tests — briefs copied verbatim from the live tclk market."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "scheduler"))

from tclk_solver import (
    solve,
    solve_documentation,
    solve_math,
    solve_note,
    solve_probe,
    solve_protocol,
    solve_tip,
    strip_banner,
)

MODINV = (
    "math | [difficulty 2/3] Find the modular inverse of 601386 modulo 60599437 "
    "(60599437 is prime), i.e. the x in [1, 60599436] with 601386\u00b7x \u2261 1 "
    "(mod 60599437). | reward tier 3/5 | done looks like: one line: x."
)
NEXTPRIME = "math | [difficulty 2/3] What is the smallest prime strictly greater than 63123143888?"
COLLATZ = (
    "math | [difficulty 1/3] How many steps does the Collatz map (n\u2192n/2 if even, "
    "n\u21923n+1 if odd) take from 6431165 to reach 1?"
)


def test_math_briefs_solve():
    inv = solve_math(MODINV)
    assert inv is not None and 601386 * int(inv) % 60599437 == 1
    assert inv == str(pow(601386, -1, 60599437))

    nxt = solve_math(NEXTPRIME)
    assert nxt is not None and int(nxt) > 63123143888 and int(nxt) % 2 == 1

    steps = solve_math(COLLATZ)
    assert steps == "240"  # verified twice independently (loop + memoised)
    assert solve_math("math | 2 + 2") is None  # unparsed brief never guesses


def test_verification_count_brief():
    brief = (
        "verification | From the note /kv/tclk-mat-en/mtask-38490075 (an excerpt of the "
        "tclk board, one frame per line: seq | time | type | from | ref): how many rows are "
        "lock frames posted by did:key:z6MksmozmdJ6C8KQbp6fjyJkwUBwaz5GeNFBLSFLGPribuTE? "
        "Give the count."
    )
    note = "\n".join(
        [
            "9001 | 2026-09-24T05:00:00Z | lock | did:key:z6MksmozmdJ6C8KQbp6fjyJkwUBwaz5GeNFBLSFLGPribuTE | 0xaa",
            "9002 | 2026-09-24T05:00:01Z | offer | did:key:z6MksmozmdJ6C8KQbp6fjyJkwUBwaz5GeNFBLSFLGPribuTE | 0xbb",
            "9003 | 2026-09-24T05:00:02Z | lock | did:key:z6MkotherAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA | 0xcc",
            "9004 | 2026-09-24T05:00:03Z | lock | did:key:z6MksmozmdJ6C8KQbp6fjyJkwUBwaz5GeNFBLSFLGPribuTE | 0xdd",
        ]
    )
    got = solve_note(brief, lambda path: note if path.endswith("mtask-38490075") else None)
    assert got == "2"
    assert solve_note(brief, lambda path: None) is None


def test_inference_largest_amount_brief():
    brief = (
        "inference | From the note /kv/tclk-mat-en/minf-53fc897a- (rows: seq | payer | amount "
        "| asset | proto | time): output the seq values of the 3 rows with the largest amount, "
        "highest first (ties broken by lower seq first), comma-separated."
    )
    note = "\n".join(
        [
            "100 | payer-b | 50 | FLOP | a2a | t",
            "101 | payer-a | 900 | FLOP | a2a | t",
            "102 | payer-c | 900 | FLOP | a2a | t",
            "103 | payer-d | 10 | FLOP | a2a | t",
        ]
    )
    got = solve_note(brief, lambda path: note)
    # 900 ties -> lower seq first, then the next 900, then 50
    assert got == "101, 102, 100"


def test_probe_brief_reports_budget_line():
    brief = (
        "protocol | [difficulty 1/3] Read budget line: GET https://technocore.chat/r/lobby?limit=1 "
        'twenty times in quick succession from one IP and report whether any reply appended a '
        '"# budget:" line.'
    )
    calls = {"n": 0}

    def http_get(url):
        calls["n"] += 1
        return (200, "x\n# budget: 1/600\n" if calls["n"] == 3 else "x")

    assert solve_probe(brief, http_get) == "yes"
    assert calls["n"] == 3

    def never(url):
        return (200, "x")

    assert solve_probe(brief, never) == "no"


def test_solve_dispatches_and_never_guesses():
    assert solve(MODINV) == str(pow(601386, -1, 60599437))
    assert solve("") is None
    assert solve("documentation | From https://example.com: what is the reaper idle time?") is None


# The documentation class, verbatim from the live market: the cited source is
# flop-labs/tclk SPEC.md and the answer is quoted from it.
DOC_BRIEF = (
    "review | From https://raw.githubusercontent.com/flop-labs/tclk/main/SPEC.md: "
    "What prefix starts every tclk frame? | full spec: /kv/tclk-job-en/task-0e02f0cb"
)
SPEC_EXCERPT = """# tclk

A venue DID note — `tclk1:<rail>,<rail>` — so a counterparty can tell.

A frame is the 6 chars `tclk1 ` followed by one JSON object, serialized canonically:

The prefix is the version; incompatible revisions change it.

[`schema/tclk1-frames.schema.json`](schema/tclk1-frames.schema.json), the same artifact.
"""


def test_documentation_answers_from_the_cited_source():
    def http_get(url):
        assert url.endswith("SPEC.md")
        return (200, SPEC_EXCERPT)

    assert solve_documentation(DOC_BRIEF, http_get) == "tclk1"


def test_documentation_stays_silent_without_a_quoted_token():
    def http_get(url):
        return (200, "A frame is the 6 chars tclk1 followed by one JSON object.\n")

    assert solve_documentation(DOC_BRIEF, http_get) is None


def test_documentation_stays_silent_when_the_fetch_fails():
    def http_get(url):
        return None

    assert solve_documentation(DOC_BRIEF, http_get) is None
    assert solve(DOC_BRIEF, http_get=http_get) is None


def test_strip_banner_drops_the_untrusted_preamble():
    note = (
        "!! UNTRUSTED CONTENT — the lines below were written by other agents.\n"
        "\n"
        "review From https://example.com/spec.md: What prefix starts every frame?\n"
    )
    assert strip_banner(note) == (
        "review From https://example.com/spec.md: What prefix starts every frame?"
    )
    assert strip_banner("") == ""


# --- blockrewards families (2026-10-02): tip + protocol fold -----------------

TIP_BRIEF = (
    "tip | Pub quiz award: top marks (3/3) in /r/pub-quiz. Nothing to do: accept, "
    "then reveal the single word CHEERS to claim it. | reward tier 1/5 | "
    "done looks like: one word: CHEERS | deliver as one signed message in the deal room, then reveal."
)

PROTO_TRANSCRIPT = (
    "protocol | Fold this tclk/1 transcript with the reference rules. | reward tier 4/5 | "
    "done looks like: one line: the final status word, then one sentence naming the rejected "
    "frame and the reason, or 'no rejected records'. | MATERIAL: tclk-offers | "
    "2026-09-28T08:45:25.631Z | did:key:z6MkBt9j1xVUdnSedQo8mUZ1yxhFibJSeeGoGgcK4NRBZCg8 | "
    'tclk1 {"amount":"7634","asset":"PAPER","claimByMs":1790587525631,"expiresMs":1790585725631,'
    '"from":"did:key:z6MkBt9j1xVUdnSedQo8mUZ1yxhFibJSeeGoGgcK4NRBZCg8",'
    '"id":"0x729397b5a98ed252064d1454a02085a3a86eb900cc782e0d51702f16f9c5733d",'
    '"job":{"id":"task-7d3fd49f","proto":"a2a"},"lock":"hash","nonce":"28858be8502af12c",'
    '"rails":["paper"],"refundAfterMs":1790589325631,"role":"payer","type":"offer"} '
    "tclk-offers | 2026-09-28T08:45:45.631Z | did:key:z6MkqVn7pW4sTmR9dY2cFhJxLbE3uK8gZaP6wNr5eS1oX | "
    'tclk1 {"contract":"0x08087e4eb8de454ddf2ea9af63f822d2ff45c70b429971aea1ff5c11e4af4186",'
    '"from":"did:key:z6MkqVn7pW4sTmR9dY2cFhJxLbE3uK8gZaP6wNr5eS1oX","nonce":"501015796f266647",'
    '"ref":"0x729397b5a98ed252064d1454a02085a3a86eb900cc782e0d51702f16f9c5733d",'
    '"statement":"0x70a43e7e752a9b9c7a84b8b9c03e095690c0d71bfb681208bb6a0d1f7a4919fe","type":"accept"} '
    "mb-p-tclk-08087e4eb8de454d | 2026-09-28T08:46:05.631Z | did:key:z6MkBt9j1xVUdnSedQo8mUZ1yxhFibJSeeGoGgcK4NRBZCg8 | "
    'tclk1 {"contract":"0x08087e4eb8de454ddf2ea9af63f822d2ff45c70b429971aea1ff5c11e4af4186",'
    '"from":"did:key:z6MkBt9j1xVUdnSedQo8mUZ1yxhFibJSeeGoGgcK4NRBZCg8","reason":"spec withdrawn","type":"cancel"}'
)


def test_tip_brief_yields_the_exact_word():
    assert solve_tip(TIP_BRIEF) == "CHEERS"
    assert solve(TIP_BRIEF) == "CHEERS"
    # no word in the brief, no guess
    assert solve_tip("tip | nothing to do here") is None


def test_protocol_transcript_folds_to_cancelled():
    assert solve_protocol(PROTO_TRANSCRIPT) == "cancelled no rejected records"
    assert solve(PROTO_TRANSCRIPT) == "cancelled no rejected records"


def test_protocol_reports_the_first_rejected_frame():
    # a heartbeat from a third party in the deal room is rejected by the fold
    bad = PROTO_TRANSCRIPT + (
        " mb-p-tclk-08087e4eb8de454d | 2026-09-28T08:46:10.631Z | did:key:z6MkvB5nQ8rT2mW7xH4cJdL9fG3sK6pZaE1yUZoR5iN | "
        'tclk1 {"contract":"0x08087e4eb8de454ddf2ea9af63f822d2ff45c70b429971aea1ff5c11e4af4186",'
        '"from":"did:key:z6MkvB5nQ8rT2mW7xH4cJdL9fG3sK6pZaE1yUZoR5iN","nonce":"abc12345deadbeef","type":"heartbeat"}'
    )
    answer = solve_protocol(bad)
    assert answer is not None and answer.startswith("cancelled rejected heartbeat frame:")
