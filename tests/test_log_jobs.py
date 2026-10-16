"""Bookkeeping beside the record: media, voice jobs, the turn ledger, the chain store (SPEC section 3, REQ-LOG-8)."""

import pytest
from tgkit import NOW, bot_message, user_message

from telegrind.incoming import incoming
from telegrind.log import Log

T0 = int(NOW.timestamp())


@pytest.fixture
def log(tmp_path):
    log = Log(tmp_path / "log.sqlite")
    yield log
    log.close()


def test_media_job_lifecycle(log):
    """REQ-MSG-2: pending → stored, attempts counted, failed and too_large kept with the entry."""
    log.record_received(incoming(user_message(photo=True), now=T0))
    log.media_enqueue(10, "p-big", "photo.jpg", "image/jpeg", 250_000)
    assert [r["message_id"] for r in log.media_pending()] == [10]
    log.media_set(10, "pending", error="HTTP 500", attempt=True)
    assert log.media_pending()[0]["attempts"] == 1 and log.media_pending()[0]["last_error"] == "HTTP 500"
    log.media_set(10, "stored", sha256="ab" * 32)
    assert log.media_pending() == []
    row = log.conn.execute("SELECT status, sha256 FROM media WHERE message_id = 10").fetchone()
    assert (row["status"], row["sha256"]) == ("stored", "ab" * 32)
    log.media_enqueue(11, "d", "big.pdf", "application/pdf", 30_000_000)
    log.media_set(11, "too_large")
    assert log.media_pending() == []


def test_voice_job_lifecycle(log):
    """REQ-VOICE-1, REQ-VOICE-8: pending → done/failed/too_long with attempts and a reason."""
    log.voice_enqueue(10, "v1")
    assert [r["message_id"] for r in log.voice_pending()] == [10]
    log.voice_set(10, "pending", error="HTTP 429", attempt=True)
    log.voice_set(10, "pending", error="HTTP 429", attempt=True)
    assert log.voice_pending()[0]["attempts"] == 2
    log.voice_set(10, "failed", error="HTTP 429")
    assert log.voice_pending() == []
    assert log.conn.execute("SELECT status, last_error FROM voice_jobs WHERE message_id = 10").fetchone()[:] == (
        "failed",
        "HTTP 429",
    )


def test_turn_ledger(log):
    """REQ-TURN-8, REQ-SCHED-1: turns are counted by kind since an instant."""
    log.turn_add("question", T0)
    log.turn_add("question", T0 + 100)
    log.turn_add("pass", T0 + 50)
    assert log.turns_since("question", T0) == 2 and log.turns_since("question", T0 + 1) == 1
    assert log.turns_since("pass", T0) == 1 and log.turns_since("pass", T0 + 51) == 0


def test_unprocessed_is_judged_by_revision(log):
    """REQ-CONTRACT-2, REQ-SCHED-1: no processed row or another revision → unprocessed; no bot rows; oldest first."""
    log.record_received(incoming(user_message(text="b", message_id=12, date=NOW), now=T0))
    log.record_received(incoming(user_message(text="a", message_id=11, date=NOW.replace(hour=1)), now=T0))
    log.record_bot_sent(500, "answer", reply_to=11, raw={}, at=T0)
    assert [e.message_id for e in log.unprocessed({})] == [11, 12]
    assert [e.message_id for e in log.unprocessed({11: 1, 12: 1})] == []
    log.record_edit(incoming(user_message(text="a2", message_id=11, edit_date=T0 + 1), now=T0 + 1))
    assert [e.message_id for e in log.unprocessed({11: 1, 12: 1})] == [11]


async def test_chain_store_runs_televibes_chain_flow(log):
    """REQ-LOG-8: the log is a ChainStore; televibe's Chains finds, links and saves through it."""
    from televibe.telegram import Chains

    chains = Chains(log)
    log.record_received(incoming(user_message(text="q?"), now=T0))
    chain = chains.new()
    assert await chains.find(user_message(message_id=11, reply_to=bot_message(message_id=500))) is None
    await chains.link(chain, bot_message(message_id=500))
    assert await chains.find(user_message(message_id=11, reply_to=bot_message(message_id=500))) == chain
    assert await log.session_of(chain.id) is None
    await log.save(chain.id, '{"dump": 1}')
    await log.save(chain.id, '{"dump": 2}')
    assert await log.session_of(chain.id) == '{"dump": 2}'
    await log.link(7, 500, "other")  # relinking replaces
    assert await log.chain_of(7, 500) == "other"
