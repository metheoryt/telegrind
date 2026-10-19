"""Controls, presses and votes are events on the bot's entry (SPEC section 3, REQ-LOG-9).

Real SQLite files in tmp_path, as test_log.py does (REQ-TEST-2)."""

import asyncio

import pytest
from televibe.telegram.controls import Form, Offer, Poll, control_block

from telegrind.log import Log

POLL_BLOCK = control_block(Poll("Что из этого займы?", ("Арман 20 000", "ничего")))
OFFER_BLOCK = control_block(Offer("Да"))
FORM_BLOCK = control_block(Form((("да", "нет"), ("a", "b")), (None, 0)))


@pytest.fixture
def store(tmp_path):
    log = Log(tmp_path / "log.sqlite")
    yield log
    log.close()


def kinds(store, message_id):
    return [e.kind for e in store.events(message_id)]


def test_control_is_open_until_answered(store):
    """REQ-LOG-9: a recorded control is open; an [offer] press closes it once; a second press closes nothing."""
    store.record_bot_sent(500, "Слить?", 10, {}, 1)
    store.record_control(500, OFFER_BLOCK, None, 2)
    assert store.control(500).open
    assert store.record_press(500, "tv:o", {"id": "q1"}, 3, closes=True) is True
    assert store.record_press(500, "tv:o", {"id": "q2"}, 4, closes=True) is False
    rec = store.control(500)
    assert not rec.open and rec.presses == ("tv:o", "tv:o") and rec.block == OFFER_BLOCK and rec.poll_id is None
    assert kinds(store, 500) == ["bot_sent", "control", "pressed", "answered", "pressed"]
    assert store.events(500)[2].payload == {"data": "tv:o", "update": {"id": "q1"}}
    assert store.entry(500).revision == 5


def test_ticks_are_kept_as_presses(store):
    """REQ-LOG-9: a tick keeps the control open; its data stays in order for replay."""
    store.record_bot_sent(500, "Что отметить?", 10, {}, 1)
    store.record_control(500, FORM_BLOCK, None, 2)
    assert store.record_press(500, "tv:t:0:1", {}, 3, closes=False) is False
    assert store.record_press(500, "tv:t:1:0", {}, 4, closes=False) is False
    rec = store.control(500)
    assert rec.open and rec.presses == ("tv:t:0:1", "tv:t:1:0")


def test_vote_resolves_the_poll_and_retraction_starts_nothing(store):
    """REQ-LOG-9: polls maps poll_id to the poll message; an empty vote is logged and closes nothing."""
    store.record_bot_sent(501, "Что из этого займы?", 500, {}, 1)
    store.record_control(501, POLL_BLOCK, "p-1", 2)
    assert store.control(501).poll_id == "p-1"
    assert store.record_vote("p-1", {"option_ids": []}, 3, closes=False) == (501, False)
    assert store.record_vote("p-1", {"option_ids": [0]}, 4, closes=True) == (501, True)
    assert store.record_vote("p-1", {"option_ids": [1]}, 5, closes=True) == (501, False)
    assert store.record_vote("nope", {}, 6, closes=True) is None
    assert kinds(store, 501) == ["bot_sent", "control", "voted", "voted", "answered", "voted"]


def test_close_and_open_controls_of_a_chain(store):
    """REQ-LOG-9: open_controls lists a chain's open ones; close_control closes once."""
    for mid in (600, 601, 602):
        store.record_bot_sent(mid, "q", 10, {}, 1)
        store.record_control(mid, OFFER_BLOCK, None, 2)
    asyncio.run(store.link(1, 600, "c"))
    asyncio.run(store.link(1, 601, "c"))
    asyncio.run(store.link(1, 602, "other"))
    store.record_press(600, "tv:o", {}, 3, closes=True)
    assert [r.message_id for r in store.open_controls("c")] == [601]
    assert store.close_control(601, "superseded", 4) is True
    assert store.close_control(601, "text", 5) is False
    assert store.close_control(600, "text", 5) is False
    assert store.open_controls("c") == []
    assert store.events(601)[-1].kind == "closed" and store.events(601)[-1].payload == {"how": "superseded"}


def test_press_on_a_message_without_a_control_stores_nothing(store):
    """REQ-LOG-9: a press on a bot message that carries no control is not an event."""
    store.record_bot_sent(502, "ok", 10, {}, 1)
    assert store.record_press(502, "tv:o", {}, 2, closes=True) is False
    assert kinds(store, 502) == ["bot_sent"]
    assert store.control(502) is None
    assert store.control(999) is None
