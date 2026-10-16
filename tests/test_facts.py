"""Read-only reads of the agent's tables (SPEC REQ-FACTS-2, REQ-SCOPE-2)."""

import sqlite3

import pytest

from telegrind.facts import Facts


def test_absent_file_reads_as_empty(tmp_path):
    """REQ-LAYOUT-1, REQ-VOICE-3: no facts.sqlite yet means no processed rows, no terms, no tz — and no file created."""
    facts = Facts(tmp_path / "facts.sqlite")
    assert not facts.exists()
    assert facts.processed() == {}
    assert facts.mark() == (0, 0)
    assert facts.keyterms() == []
    assert facts.tz() is None
    assert not (tmp_path / "facts.sqlite").exists()


def test_reads_the_three_tables(tmp_path):
    """REQ-FACTS-2: processed, keyterms, settings.tz."""
    conn = sqlite3.connect(tmp_path / "facts.sqlite")
    conn.executescript(
        "CREATE TABLE processed(entry_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, at INTEGER NOT NULL);"
        "CREATE TABLE keyterms(term TEXT PRIMARY KEY);"
        "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO processed VALUES (10, 1, 1), (11, 3, 2); INSERT INTO keyterms VALUES ('такси'), ('Kaspi');"
        "INSERT INTO settings VALUES ('tz', 'Asia/Bangkok');"
    )
    conn.commit()
    conn.close()
    facts = Facts(tmp_path / "facts.sqlite")
    assert facts.exists()
    assert facts.processed() == {10: 1, 11: 3}
    assert facts.mark() == (2, 3)
    assert facts.keyterms() == ["Kaspi", "такси"]
    assert facts.tz() == "Asia/Bangkok"


def test_missing_tables_are_empty_not_errors(tmp_path):
    """REQ-FACTS-2: a facts.sqlite the agent half-made reads as empty."""
    sqlite3.connect(tmp_path / "facts.sqlite").close()
    facts = Facts(tmp_path / "facts.sqlite")
    assert facts.processed() == {}
    assert facts.keyterms() == []
    assert facts.tz() is None
    assert facts.mark() == (0, 0)


def test_opens_read_only(tmp_path):
    """REQ-SCOPE-2: the connection is mode=ro; a write through it fails."""
    sqlite3.connect(tmp_path / "facts.sqlite").close()
    facts = Facts(tmp_path / "facts.sqlite")
    conn = facts._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x(a)")
    finally:
        conn.close()
