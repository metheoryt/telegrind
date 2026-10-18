"""Read-only reads of the agent's tables (SPEC REQ-FACTS-2, REQ-SCOPE-2)."""

import sqlite3
import subprocess
import sys
import textwrap

import pytest

from telegrind.facts import Facts, FactsUnavailable

TABLES = (
    "CREATE TABLE processed(entry_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, at INTEGER NOT NULL);"
    "CREATE TABLE keyterms(term TEXT PRIMARY KEY);"
    "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
)


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


def _all_reads(facts: Facts) -> list:
    return [facts.processed, facts.mark, facts.keyterms, facts.tz, facts.reminders]


def test_a_hot_journal_is_unavailable_not_empty(tmp_path):
    """A writer killed mid-transaction leaves a hot journal; a read-only reader cannot roll it back.

    That must not read as an empty table: every read raises FactsUnavailable naming the error type."""
    path = tmp_path / "facts.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(TABLES)
    conn.executemany("INSERT INTO processed VALUES (?, 1, 1)", [(i,) for i in range(2000)])
    conn.commit()
    conn.close()
    killed_writer = textwrap.dedent(f"""
        import os, sqlite3
        c = sqlite3.connect({str(path)!r}, isolation_level=None)
        c.execute("PRAGMA cache_size=1")  # spill to the file, so the journal is needed to undo it
        c.execute("BEGIN")
        c.execute("UPDATE processed SET revision = revision + 1")
        os._exit(0)
    """)
    subprocess.run([sys.executable, "-c", killed_writer], check=True, timeout=30)
    journal = tmp_path / "facts.sqlite-journal"
    assert journal.is_file() and journal.stat().st_size > 0  # the condition is real, not assumed
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        raw.execute("SELECT count(*) FROM processed").fetchall()
    raw.close()
    facts = Facts(path, timeout=0.05)
    for read in _all_reads(facts):
        with pytest.raises(FactsUnavailable) as caught:
            read()
        assert caught.value.reason == "OperationalError"


def test_a_locked_file_is_unavailable_not_empty(tmp_path):
    """A writer holding an exclusive lock: `database is locked` is FactsUnavailable, not (0, 0)."""
    path = tmp_path / "facts.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(TABLES)
    conn.close()
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        facts = Facts(path, timeout=0.05)
        for read in _all_reads(facts):
            with pytest.raises(FactsUnavailable):
                read()
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert Facts(path).mark() == (0, 0)  # once released, the same file reads normally
