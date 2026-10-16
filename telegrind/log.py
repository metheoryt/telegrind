"""The log: what Telegram sent, and nothing derived (SPEC section 3)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegrind.incoming import AUTHOR_BOT, Incoming

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, at INTEGER NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_message ON events(message_id, id);
CREATE TABLE IF NOT EXISTS entries(
    message_id INTEGER PRIMARY KEY, author TEXT NOT NULL, kind TEXT NOT NULL,
    occurred_at INTEGER NOT NULL, received_at INTEGER NOT NULL, edited_at INTEGER,
    text TEXT, transcript_raw TEXT, transcript TEXT, media_sha256 TEXT, media_name TEXT,
    reply_to INTEGER, raw TEXT NOT NULL, receipt TEXT, deleted_at INTEGER,
    revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media(
    message_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, sha256 TEXT, name TEXT, mime TEXT, size INTEGER,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS voice_jobs(
    message_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS chain_links(message_id INTEGER PRIMARY KEY, chain_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain_sessions(chain_id TEXT PRIMARY KEY, session TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns(id INTEGER PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL);
"""

ENTRY_COLUMNS = (
    "message_id",
    "author",
    "kind",
    "occurred_at",
    "received_at",
    "edited_at",
    "text",
    "transcript_raw",
    "transcript",
    "media_sha256",
    "media_name",
    "reply_to",
    "raw",
    "receipt",
    "deleted_at",
    "revision",
)
SELECT_ENTRY = (
    "SELECT " + ", ".join(ENTRY_COLUMNS) + " FROM entries WHERE message_id = ?"  # noqa: S608 - column names are a constant
)


def now() -> int:
    return int(time.time())


@dataclass(frozen=True, slots=True)
class Entry:
    message_id: int
    author: str
    kind: str
    occurred_at: int
    received_at: int
    edited_at: int | None
    text: str | None
    transcript_raw: str | None
    transcript: str | None
    media_sha256: str | None
    media_name: str | None
    reply_to: int | None
    raw: str
    receipt: str | None
    deleted_at: int | None
    revision: int


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    message_id: int
    at: int
    kind: str
    payload: dict[str, Any]


class Log:
    """The connection is used from the event-loop thread only; no method is thread-safe.

    `check_same_thread=False` exists solely so a test can release a lock from a `threading.Timer`.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection]:
        """One transaction; BEGIN IMMEDIATE takes the write lock up front so a reader never upgrades mid-way."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            # A failed COMMIT leaves the transaction open; roll back so the connection is usable again.
            # A ROLLBACK failure must not mask the original exception.
            if self.conn.in_transaction:
                with suppress(sqlite3.Error):
                    self.conn.execute("ROLLBACK")
            raise

    # -- events and entries -----------------------------------------------------------

    def _append(self, message_id: int, kind: str, payload: dict[str, Any], at: int) -> None:
        """Inside a transaction: add the event and set revision to the event count (REQ-LOG-3)."""
        self.conn.execute(
            "INSERT INTO events(message_id, at, kind, payload) VALUES (?, ?, ?, ?)",
            (message_id, at, kind, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.execute(
            "UPDATE entries SET revision = (SELECT count(*) FROM events WHERE message_id = ?) WHERE message_id = ?",
            (message_id, message_id),
        )

    def record_received(self, inc: Incoming) -> Entry:
        with self._tx():
            self.conn.execute(
                "INSERT INTO entries("
                "message_id, author, kind, occurred_at, received_at, edited_at, text, reply_to, raw, revision)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    inc.message_id,
                    inc.author,
                    inc.kind,
                    inc.occurred_at,
                    inc.received_at,
                    inc.edited_at,
                    inc.text,
                    inc.reply_to,
                    json.dumps(inc.raw, ensure_ascii=False),
                ),
            )
            self._append(inc.message_id, "received", inc.raw, inc.received_at)
        return self._entry(inc.message_id)

    def record_edit(self, inc: Incoming) -> Entry | None:
        with self._tx():
            if self._exists(inc.message_id) is None:
                return None
            self.conn.execute(
                "UPDATE entries SET text = ?, edited_at = ?, raw = ? WHERE message_id = ?",
                (
                    inc.text,
                    inc.edited_at if inc.edited_at is not None else inc.received_at,
                    json.dumps(inc.raw, ensure_ascii=False),
                    inc.message_id,
                ),
            )
            self._append(inc.message_id, "edited", inc.raw, inc.received_at)
        return self._entry(inc.message_id)

    def record_reaction(self, message_id: int, deleted: bool, payload: dict[str, Any], at: int) -> Entry | None:
        with self._tx():
            row = self._exists(message_id)
            if row is None or (row["deleted_at"] is not None) == deleted:
                return None  # unknown, or no change (REQ-REACT-2)
            self.conn.execute(
                "UPDATE entries SET deleted_at = ? WHERE message_id = ?", (at if deleted else None, message_id)
            )
            self._append(message_id, "deleted" if deleted else "restored", payload, at)
        return self._entry(message_id)

    def record_stored(
        self, message_id: int, sha256: str, name: str | None, mime: str | None, size: int, at: int
    ) -> Entry | None:
        with self._tx():
            if self._exists(message_id) is None:
                return None
            self.conn.execute(
                "UPDATE entries SET media_sha256 = ?, media_name = ? WHERE message_id = ?", (sha256, name, message_id)
            )
            self._append(message_id, "stored", {"sha256": sha256, "name": name, "mime": mime, "size": size}, at)
        return self._entry(message_id)

    def record_transcript(
        self, message_id: int, raw: str, text: str, question: bool, cleaned: bool, at: int
    ) -> Entry | None:
        with self._tx():
            if self._exists(message_id) is None:
                return None
            self.conn.execute(
                "UPDATE entries SET transcript_raw = ?, transcript = ? WHERE message_id = ?", (raw, text, message_id)
            )
            self._append(
                message_id, "transcribed", {"raw": raw, "text": text, "question": question, "cleaned": cleaned}, at
            )
        return self._entry(message_id)

    def record_bot_sent(self, message_id: int, text: str, reply_to: int | None, raw: dict[str, Any], at: int) -> Entry:
        with self._tx():
            self.conn.execute(
                "INSERT INTO entries(message_id, author, kind, occurred_at, received_at, text, reply_to, raw, revision)"
                " VALUES (?, ?, 'text', ?, ?, ?, ?, ?, 0)",
                (message_id, AUTHOR_BOT, at, at, text, reply_to, json.dumps(raw, ensure_ascii=False)),
            )
            self._append(message_id, "bot_sent", raw, at)
        return self._entry(message_id)

    def set_receipt(self, message_id: int, emoji: str | None) -> None:
        with self._tx():
            self.conn.execute("UPDATE entries SET receipt = ? WHERE message_id = ?", (emoji, message_id))

    # -- reads ------------------------------------------------------------------------

    def _exists(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT deleted_at FROM entries WHERE message_id = ?", (message_id,)).fetchone()

    def _entry(self, message_id: int) -> Entry:
        row = self.conn.execute(SELECT_ENTRY, (message_id,)).fetchone()
        return Entry(*row)

    def entry(self, message_id: int) -> Entry | None:
        return self._entry(message_id) if self._exists(message_id) is not None else None

    def events(self, message_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT id, message_id, at, kind, payload FROM events WHERE message_id = ? ORDER BY id", (message_id,)
        )
        return [Event(r["id"], r["message_id"], r["at"], r["kind"], json.loads(r["payload"])) for r in rows]

    def author_of(self, message_id: int | None) -> str | None:
        if message_id is None:
            return None
        row = self.conn.execute("SELECT author FROM entries WHERE message_id = ?", (message_id,)).fetchone()
        return row["author"] if row else None
