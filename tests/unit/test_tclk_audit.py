"""tclk/1 offer security audit tests."""

from connectors.tclk import _risk_ok, audit_offer, offer_allows, parse_frame

RAILS, CAP, PAT = "flop-htlc,x402", "1000000", "market,scan,digest,report,summary,stats,read,observe"

SPECLESS = 'tclk1 {"type":"offer","amount":"200","asset":"FLOP","nonce":"ff2b65b6ae51c809"}'
SPECCED = (
    'tclk1 {"type":"offer","amount":"800","asset":"FLOP","rail":"flop-htlc",'
    '"nonce":"aa11","spec":"scan tclk-offers and report stats"}'
)


def _audit(raw, *, signed=True, specless=False, rails=RAILS, cap=CAP, pat=PAT):
    return audit_offer(
        parse_frame(raw, signed=signed),
        allowed_rails=rails,
        max_amount=cap,
        patterns=pat,
        accept_specless=specless,
    )


def test_specless_offer_is_skipped_without_optin():
    """The market's bulk offer (no spec) must stay OUT unless the operator opts in."""
    a = _audit(SPECLESS, specless=False)
    assert a["decision"] == "skip"
    assert a["risk"] == "risky"
    assert a["spec_missing"] is True
    assert "(empty)" in a["reason"]


def test_specless_offer_accepted_under_optin_but_flagged_watch():
    """Opt-in accepts it, yet the missing spec is still recorded — never silently 'safe'."""
    a = _audit(SPECLESS, specless=True)
    assert a["decision"] == "accept"
    assert a["risk"] == "watch"
    assert a["spec_missing"] is True


def test_specced_matching_offer_is_safe():
    a = _audit(SPECCED)
    assert a["decision"] == "accept"
    assert a["risk"] == "safe"
    assert a["spec_missing"] is False


def test_audit_risk_tiers():
    # unsigned → dangerous: no signature, no commitment
    assert _audit(SPECCED, signed=False)["risk"] == "dangerous"
    # non-offer → dangerous
    assert _audit('tclk1 {"type":"accept","ref":"0xaa11","statement":"0xbb"}')["risk"] == "dangerous"
    # rail outside the allow-list → risky (bounded, not hostile)
    eth = 'tclk1 {"type":"offer","amount":"800","asset":"ETH","rail":"ETH","nonce":"b1","spec":"scan"}'
    assert _audit(eth)["risk"] == "risky"
    # over cap → risky
    big = 'tclk1 {"type":"offer","amount":"5000000","asset":"FLOP","rail":"flop-htlc","nonce":"b2","spec":"scan"}'
    assert _audit(big)["risk"] == "risky"
    # unparseable amount fails closed
    bad = 'tclk1 {"type":"offer","amount":"lots","asset":"FLOP","rail":"flop-htlc","nonce":"b4","spec":"scan"}'
    assert _audit(bad)["decision"] == "skip"


def test_audit_checks_are_recorded():
    a = _audit(SPECLESS, specless=True)
    checks = a["checks"]
    assert checks["kind"] == "offer"
    assert checks["signed"] is True
    assert checks["decision"] == "accept"
    assert checks["risk"] == "watch"
    assert checks["amount"] == "200"
    # the human-readable reason always travels with the record
    assert a["reason"]


def test_risk_ok_ceiling():
    # rank: safe < watch < risky < dangerous
    assert _risk_ok("safe", "safe") is True
    assert _risk_ok("watch", "safe") is False
    assert _risk_ok("watch", "watch") is True
    assert _risk_ok("risky", "watch") is False
    assert _risk_ok("dangerous", "risky") is False
    assert _risk_ok("safe", "dangerous") is True
    # unknown values fail closed
    assert _risk_ok("nonsense", "safe") is False
    assert _risk_ok("safe", "nonsense") is False


def test_offer_allows_backcompat_default_is_unchanged():
    """Existing callers pass 4 positional args; behaviour must not shift."""
    f = parse_frame(SPECLESS, signed=True)
    assert offer_allows(f, RAILS, CAP, PAT)[0] is False  # spec-less still refused by default
    assert offer_allows(f, RAILS, CAP, PAT, True)[0] is True  # explicit opt-in
    assert offer_allows(parse_frame(SPECCED, signed=True), RAILS, CAP, PAT)[0] is True
