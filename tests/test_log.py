"""The log: events only grow, entries hold the current state (SPEC section 3)."""

import json
import sqlite3
import threading

import pytest
from tgkit import NOW, user_message

from telegrind.incoming import incoming
from telegrind.log import Log

T0 = int(NOW.timestamp())


@pytest.fixture
def log(tmp_path):
    log = Log(tmp_path / "log.sqlite")
    yield log
    log.close()


def test_received_writes_event_and_entry_in_one_transaction(log):
    """REQ-LOG-1, REQ-LOG-3, REQ-LOG-5: one event, one entry, revision 1, raw is the dump."""
    entry = log.record_received(incoming(user_message(text="4500 такси"), now=T0 + 1))
    assert entry.message_id == 10 and entry.text == "4500 такси" and entry.revision == 1 and entry.receipt is None
    assert entry.occurred_at == T0 and entry.received_at == T0 + 1 and entry.deleted_at is None
    events = log.events(10)
    assert [e.kind for e in events] == ["received"] and events[0].payload["text"] == "4500 такси"
    assert json.loads(entry.raw)["message_id"] == 10


def test_two_edits_keep_all_three_texts(log):
    """REQ-LOG-2, REQ-LOG-4: nothing is overwritten in events; the entry has the latest text and edited_at."""
    log.record_received(incoming(user_message(text="v1"), now=T0))
    log.record_edit(incoming(user_message(text="v2", edit_date=T0 + 10), now=T0 + 10))
    entry = log.record_edit(incoming(user_message(text="v3", edit_date=T0 + 20), now=T0 + 20))
    assert entry.text == "v3" and entry.edited_at == T0 + 20 and entry.revision == 3
    assert [e.payload["text"] for e in log.events(10)] == ["v1", "v2", "v3"]
    assert [e.kind for e in log.events(10)] == ["received", "edited", "edited"]


def test_edit_of_unknown_message_writes_nothing(log):
    """REQ-MSG-3: an edit of a message the log never saw is not a new entry."""
    assert log.record_edit(incoming(user_message(text="v2", edit_date=T0), now=T0)) is None
    assert log.entry(10) is None


def test_revision_counts_events_of_every_kind(log):
    """REQ-LOG-3: after every kind of event revision == count of the message's events."""
    log.record_received(incoming(user_message(voice=5), now=T0))
    log.record_transcript(10, raw="четыре пятьсот такси", text="4500 такси", question=False, cleaned=True, at=T0 + 5)
    log.record_edit(incoming(user_message(voice=5, caption="c", edit_date=T0 + 6), now=T0 + 6))
    log.record_reaction(10, deleted=True, payload={"new_reaction": [{"type": "emoji", "emoji": "💔"}]}, at=T0 + 7)
    log.record_reaction(10, deleted=False, payload={"new_reaction": []}, at=T0 + 8)
    entry = log.record_stored(10, sha256="ab" * 32, name="voice.ogg", mime="audio/ogg", size=10, at=T0 + 9)
    kinds = [e.kind for e in log.events(10)]
    assert kinds == ["received", "transcribed", "edited", "deleted", "restored", "stored"]
    assert entry.revision == 6 == len(kinds)
    assert entry.transcript_raw == "четыре пятьсот такси" and entry.transcript == "4500 такси"
    assert entry.media_sha256 == "ab" * 32 and entry.media_name == "voice.ogg"


def test_deleted_and_restored(log):
    """REQ-LOG-4, REQ-REACT-1, REQ-REACT-2: deleted_at set then cleared; a repeat writes nothing."""
    log.record_received(incoming(user_message(text="x"), now=T0))
    e1 = log.record_reaction(10, deleted=True, payload={}, at=T0 + 1)
    assert e1.deleted_at == T0 + 1 and e1.revision == 2
    assert log.record_reaction(10, deleted=True, payload={}, at=T0 + 2) is None
    assert log.entry(10).revision == 2
    e2 = log.record_reaction(10, deleted=False, payload={}, at=T0 + 3)
    assert e2.deleted_at is None and e2.revision == 3
    assert log.record_reaction(10, deleted=False, payload={}, at=T0 + 4) is None
    assert log.record_reaction(99, deleted=True, payload={}, at=T0) is None


def test_forward_is_dated_by_its_origin(log):
    """REQ-LOG-4, REQ-MSG-4: occurred_at is the origin's date, author names the source."""
    entry = log.record_received(incoming(user_message(text="receipt", forward=("user", "Vasya Pupkin")), now=T0))
    assert entry.occurred_at == T0 - 7 * 86400 and entry.author == "forward: Vasya Pupkin"


def test_receipt_is_remembered_without_an_event(log):
    """REQ-RECEIPT-3: entries.receipt records what the bot last set; it is not a revision."""
    log.record_received(incoming(user_message(text="x"), now=T0))
    log.set_receipt(10, "💔")
    assert log.entry(10).receipt == "💔" and log.entry(10).revision == 1
    log.set_receipt(10, None)
    assert log.entry(10).receipt is None


def test_bot_message_is_stored_like_anyones(log):
    """REQ-LOG-7: a bot_sent event, author bot, no receipt; reply_to resolves."""
    log.record_received(incoming(user_message(text="q?"), now=T0))
    entry = log.record_bot_sent(500, text="12 000 тенге", reply_to=10, raw={"message_id": 500}, at=T0 + 3)
    assert entry.author == "bot" and entry.kind == "text" and entry.reply_to == 10 and entry.receipt is None
    assert log.author_of(500) == "bot" and log.author_of(10) == "me" and log.author_of(1) is None
    assert [e.kind for e in log.events(500)] == ["bot_sent"]


def test_wal_and_a_concurrent_reader(log, tmp_path):
    """REQ-LOG-6: WAL with a busy timeout; a read transaction on another connection blocks no write."""
    assert log.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert log.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    log.record_received(incoming(user_message(text="x"), now=T0))
    reader = sqlite3.connect(f"file:{tmp_path / 'log.sqlite'}?mode=ro", uri=True, isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    log.record_received(incoming(user_message(text="y", message_id=11), now=T0))  # must not raise or wait 5 s
    assert reader.execute("SELECT count(*) FROM entries").fetchone()[0] == 1  # snapshot isolation
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
    reader.close()


def test_a_held_write_waits_within_the_timeout(log, tmp_path):
    """REQ-LOG-6: a writer that finds the file locked waits rather than failing at once."""
    other = sqlite3.connect(tmp_path / "log.sqlite", isolation_level=None, timeout=5, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.3, lambda: other.execute("COMMIT"))
    release.start()
    log.record_received(incoming(user_message(text="x"), now=T0))  # waits ~0.3 s, then writes
    release.join()
    assert log.entry(10) is not None
    other.close()
