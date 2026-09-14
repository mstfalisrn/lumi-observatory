"""tclk/1 frame parser/builder/validation tests."""

from connectors.tclk import (
    build_offer,
    parse_frame,
    validate_frame,
)

OFFER = 'tclk1 {"type":"offer","amount":"1000000","asset":"FLOP","nonce":"9f2c81d0","rail":"flop-htlc","spec":"/kv/specs/build-thing.md"}'
ACCEPT = 'tclk1 {"type":"accept","ref":"0x864bc2471ec70f5cc191117d74381b0c","statement":"0xdeadbeef","contract":"0x7e3e78dc785c682e45da760c2a13ae6d1de9fe26a5e3492a2d181fb5a330bd4f"}'
LOCK = 'tclk1 {"type":"lock","contract":"0x7e3e78dc","ref":"rail-xyz-1","rail":"flop-htlc"}'
REVEAL = 'tclk1 {"type":"reveal","contract":"0x7e3e78dc","secret":"0x5f4dcc3b5aa765d61d8327deb882cf99"}'
UNSIGNED_HELLO = "hello world, just chatting"


def test_parse_valid_frames():
    for line, kind in ((OFFER, "offer"), (ACCEPT, "accept"), (LOCK, "lock"), (REVEAL, "reveal")):
        f = parse_frame(line, author="did:key:z6MkTest", signed=True)
        assert f is not None, line
        assert f.kind == kind
        assert f.signed is True
        assert f.author == "did:key:z6MkTest"


def test_parse_rejects_garbage():
    assert parse_frame(UNSIGNED_HELLO) is None
    assert parse_frame("") is None
    assert parse_frame("tclk1 not-json") is None
    assert parse_frame('tclk1 {"type":"unknown"}') is None
    assert parse_frame('tclk1 ["not","a","dict"]') is None


def test_deal_room_derivation():
    f = parse_frame(ACCEPT)
    assert f.deal_room() == "mb-p-tclk-7e3e78dc785c682e"


def test_validate_rules():
    f = parse_frame(OFFER)
    assert validate_frame(f) == []  # well-formed offer
    bad = parse_frame('tclk1 {"type":"offer","asset":"FLOP"}')  # missing amount
    assert "offer missing amount" in validate_frame(bad)
    assert "offer missing asset" in validate_frame(parse_frame('tclk1 {"type":"offer","amount":"1"}'))
    a = parse_frame(ACCEPT)
    assert validate_frame(a) == []
    assert validate_frame(parse_frame('tclk1 {"type":"accept"}'))  # missing ref+statement
    assert "accept missing ref" in validate_frame(parse_frame('tclk1 {"type":"accept"}'))


def test_safe_summary_never_leaks_reveal_secret():
    f = parse_frame(REVEAL)
    assert "0x5f4dcc3b5aa765d61d8327deb882cf99" not in f.safe_summary()
    assert "reveal" in f.safe_summary()
    assert "contract" not in f.safe_summary() or "0x7e3e78dc" in f.safe_summary()


def test_signed_lane_semantics():
    # an unsigned frame is data, not a commitment — caller passes signed=False
    f = parse_frame(OFFER, signed=False)
    assert f is not None
    assert f.signed is False


def test_build_offer_roundtrip():
    line = build_offer("250000", "FLOP", nonce="abc123", spec="/kv/specs/x.md", rail="flop-htlc")
    assert line.startswith("tclk1 ")
    f = parse_frame(line)
    assert f is not None
    assert f.kind == "offer"
    assert f.amount == "250000"
    assert f.asset == "FLOP"
    assert f.rail == "flop-htlc"
    assert validate_frame(f) == []