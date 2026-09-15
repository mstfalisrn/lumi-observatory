"""tclk/1 frame parser/builder/validation tests."""

import hashlib

from connectors.tclk import (
    build_accept,
    build_offer,
    build_reveal,
    new_hashlock,
    offer_allows,
    offer_expired,
    parse_frame,
    ref_matches,
    spec_short,
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


# --- safe-agent additions -------------------------------------------------


def test_new_hashlock_statement_matches_preimage():
    preimage, statement = new_hashlock()
    assert preimage.startswith("0x") and len(preimage) == 66  # 32 bytes hex
    assert statement.startswith("0x") and len(statement) == 66
    # the statement must be sha256 of the raw preimage bytes
    assert statement == f"0x{hashlib.sha256(bytes.fromhex(preimage[2:])).hexdigest()}"
    # two mints never collide
    assert new_hashlock()[1] != statement


def test_build_accept_and_reveal_roundtrip():
    a = parse_frame(build_accept("0xabc123abc123abc1", "0x" + "11" * 32))
    assert a is not None and a.kind == "accept"
    assert a.ref == "0xabc123abc123abc1"
    assert validate_frame(a) == []
    r = parse_frame(build_reveal("0x" + "22" * 32))
    assert r is not None and r.kind == "reveal"
    assert validate_frame(r) == []
    # the reveal secret is never part of the masked summary
    assert "22" * 32 not in r.safe_summary()


def test_offer_allows_gating():
    rails, cap, pat = "flop-htlc,x402", "1000000", "market,scan,digest"
    good = parse_frame(
        'tclk1 {"type":"offer","amount":"800","asset":"FLOP","rail":"flop-htlc",'
        '"nonce":"aa11","spec":"scan tclk-offers and report stats"}',
        signed=True,
    )
    ok, why = offer_allows(good, rails, cap, pat)
    assert ok, why

    # unsigned offer is never a commitment
    assert offer_allows(parse_frame(good.raw, signed=False), rails, cap, pat)[0] is False
    # rail not allowed
    eth = parse_frame('tclk1 {"type":"offer","amount":"800","asset":"ETH","rail":"ETH","nonce":"b1","spec":"scan"}')
    assert offer_allows(eth, rails, cap, pat)[0] is False
    # over the amount cap
    big = parse_frame('tclk1 {"type":"offer","amount":"5000000","asset":"FLOP","rail":"flop-htlc","nonce":"b2","spec":"scan"}')
    assert offer_allows(big, rails, cap, pat)[0] is False
    # task outside our capabilities
    other = parse_frame('tclk1 {"type":"offer","amount":"800","asset":"FLOP","rail":"flop-htlc","nonce":"b3","spec":"paint a mural"}')
    assert offer_allows(other, rails, cap, pat)[0] is False
    # unparseable amount fails closed
    bad = parse_frame('tclk1 {"type":"offer","amount":"lots","asset":"FLOP","rail":"flop-htlc","nonce":"b4","spec":"scan"}')
    assert offer_allows(bad, rails, cap, pat)[0] is False


def test_offer_expired_conservative():
    fresh = parse_frame('tclk1 {"type":"offer","amount":"1","asset":"FLOP","nonce":"d1"}')
    assert offer_expired(fresh) is False  # no deadline field → not expired
    past = parse_frame('tclk1 {"type":"offer","amount":"1","asset":"FLOP","nonce":"d2","expiresMs":"1700000000000"}')
    assert offer_expired(past) is True
    future_ms = int(__import__("time").time() * 1000) + 3_600_000
    soon = parse_frame(f'tclk1 {{"type":"offer","amount":"1","asset":"FLOP","nonce":"d3","expiresMs":"{future_ms}"}}')
    assert offer_expired(soon) is False


def test_ref_matches_normalizes_and_guards_short_values():
    assert ref_matches("0xABCDEF0123456789", "abcdef0123456789") is True
    # suffix overlap of >=12 hex chars matches (lock refs may be truncated ids)
    assert ref_matches("0x" + "aa" * 20, "aa" * 12) is True
    assert ref_matches("0xdeadbeef", "0xdeadbeef") is True
    assert ref_matches("", "0xdeadbeef") is False
    assert ref_matches("0x" + "aa" * 20, "bb" * 12) is False
    # short values must not match by suffix (avoid false positives)
    assert ref_matches("0xaa11", "aa11") is True  # exact match is allowed
    assert ref_matches("0xffaa11", "aa11") is False  # too short for suffix rule


def test_lock_and_reveal_derive_same_deal_room():
    """The claim radar pairs lock+reveal by derived deal room — that equality is the core assumption."""
    lock = parse_frame('tclk1 {"type":"lock","contract":"0xaabbccddeeff00112233","rail":"flop-htlc","ref":"r1"}')
    reveal = parse_frame('tclk1 {"type":"reveal","contract":"0xaabbccddeeff00112233445566778899","secret":"0x00"}')
    assert lock.deal_room() == reveal.deal_room() == "mb-p-tclk-aabbccddeeff0011"


def test_spec_short_never_leaks_and_truncates():
    f = parse_frame('tclk1 {"type":"offer","amount":"1","asset":"FLOP","spec":"scan MARKET and digest it"}')
    s = spec_short(f)
    assert s == "scan market and digest it"
    long = parse_frame('tclk1 {"type":"offer","amount":"1","asset":"FLOP","spec":"' + "x" * 200 + '"}')
    assert len(spec_short(long)) == 48