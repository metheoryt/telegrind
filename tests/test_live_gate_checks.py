"""The live gate's own checks, on a real log with no agent: they must see what they claim to (REQ-TEST-4)."""

import sqlite3

import pytest
import test_live_gate as gate
from tgkit import FakeBot

from telegrind.app import App
from telegrind.config import Config
from telegrind.layout import Layout


@pytest.fixture
def seeded(tmp_path):
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path)})
    app = App(config=cfg, bot=FakeBot(), layout=Layout(tmp_path))
    ids = gate._seed(app, 7)
    yield app, app.layout.log_path(7), set(ids.values())
    app.close()


def _write(path, sql: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(sql)
    conn.commit()
    conn.close()


def test_pythons_own_appends_are_not_changes(seeded):
    """REQ-FACTS-1: the turn ledger, a bot message and a new entry are what Python writes during a pass or question."""
    app, path, ids = seeded
    before = gate._log_snapshot(path)
    store = app.log_for(7)
    store.turn_add("pass", 1)
    store.record_bot_sent(501, "digest", None, {}, 2)
    assert gate._log_changes(before, gate._log_snapshot(path), ids) == []


@pytest.mark.parametrize(
    ("sql", "names"),
    [
        ("UPDATE entries SET text = 'x' WHERE message_id = 10", "entries"),
        ("DELETE FROM events WHERE message_id = 10", "events"),
        ("INSERT INTO media(message_id, file_id, status) VALUES (999, 'f', 'stored')", "media"),
        ("INSERT INTO voice_jobs(message_id, file_id, status) VALUES (999, 'f', 'done')", "voice_jobs"),
        ("INSERT INTO events(message_id, at, kind, payload) VALUES (10, 1, 'edited', '{}')", "events"),
        ("CREATE TABLE junk(x)", "junk"),
        ("CREATE INDEX junk_idx ON entries(text)", "junk_idx"),
        ("CREATE VIEW junk_view AS SELECT * FROM entries", "junk_view"),
        ("CREATE TRIGGER junk_trg AFTER INSERT ON turns BEGIN SELECT 1; END", "junk_trg"),
    ],
)
def test_any_other_write_is_a_change_naming_its_object(seeded, sql, names):
    """REQ-FACTS-1: an update, a delete, an insert outside Python's append tables, a row for a seeded entry, and any
    schema object are all reported, by name."""
    _, path, ids = seeded
    before = gate._log_snapshot(path)
    _write(path, sql)
    changes = gate._log_changes(before, gate._log_snapshot(path), ids)
    assert changes and any(names in c for c in changes), changes


@pytest.mark.parametrize(("ok", "runs", "passes"), [(2, 3, True), (3, 3, True), (1, 3, False), (1, 1, True)])
def test_the_default_rate_is_two_runs_of_three(ok, runs, passes):
    """REQ-TEST-4: the default rate admits 2 of 3 runs, not only 3 of 3."""
    assert pytest.approx(2 / 3) == gate.RATE
    assert (ok >= gate._needed(runs)) is passes


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        ("На такси ушло 5700 ₸.", True),
        ("На такси ушло **5 700 ₸**.", True),
        ("На такси ушло 5 700 KZT", True),
        ("На такси ушло 5,700 KZT", True),
        ("Итого −5 700", True),
        ("Одна трата: 4500 такси", False),
        ("57000 тенге", False),
        ("15 700", False),
    ],
)
def test_the_answer_must_state_the_sum(answer, says):
    """REQ-CONTRACT-7: the number, with or without a thousands separator or a sign, and not a longer number."""
    assert gate._says(answer, 5700) is says
