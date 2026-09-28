"""Lane-aware accept policy: which lane an offer is in, and whether it may be posted.

Two lanes do not share a budget. The general accept cap is our own brake on
public commitments; validation offers keep a reserve of their own on top of it,
because they are the cheapest scoring lane (+6 for a one-line verdict, no escrow,
no reveal) and general traffic must not be able to spend it.
"""

from connectors.tclk import accept_room, offer_family, parse_frame


def _offer(job_id: str) -> object:
    spec = f'a2a kv tclk-job-4f {job_id}'
    frame = parse_frame(
        "tclk1 "
        + '{"type":"offer","amount":"1000000","asset":"FLOP","nonce":"9f2c81d0",'
        + f'"rails":["paper"],"spec":"{spec}"}}'
    )
    assert frame is not None
    return frame


def test_offer_family_reads_the_lane_from_the_job_id():
    assert offer_family(_offer("val-68b26243")) == "validation"
    assert offer_family(_offer("inf-e702aacf")) == "inference"
    assert offer_family(_offer("job-076d6b03")) == "task"


def test_offer_family_ignores_words_that_merely_contain_the_prefix():
    # "interval-44aa" / "value-e702aacf" are not lane ids: the marker needs its own
    # boundary, or every job id containing the letters would look like a lane.
    assert offer_family(_offer("interval-44aa")) == "task"
    assert offer_family(_offer("value-e702aacf")) == "task"


def test_offer_family_of_a_frame_without_a_lane_is_task():
    frame = parse_frame('tclk1 {"type":"offer","amount":"5","asset":"FLOP","nonce":"ab"}')
    assert frame is not None
    assert offer_family(frame) == "task"


def test_accept_room_stops_at_the_cap_without_a_reserve():
    assert accept_room(30, 29) is True
    assert accept_room(30, 30) is False
    assert accept_room(30, 31) is False


def test_accept_room_keeps_a_lane_alive_past_the_cap():
    # Cap spent, validation lane still has its 12 reserved slots.
    assert accept_room(30, 30, reserve=12) is True
    assert accept_room(30, 41, reserve=12) is True
    assert accept_room(30, 42, reserve=12) is False


def test_accept_room_never_goes_negative():
    assert accept_room(30, 0, reserve=-5) is True
    assert accept_room(30, 30, reserve=-5) is False


def test_a_non_positive_cap_disables_accepting():
    assert accept_room(0, 0, reserve=12) is False
    assert accept_room(-1, 0, reserve=12) is False
