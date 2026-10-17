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

    def record_transcript(self, message_id: int, raw: str, text: str, cleaned: bool, at: int) -> Entry | None:
        with self._tx():
            if self._exists(message_id) is None:
                return None
            self.conn.execute(
                "UPDATE entries SET transcript_raw = ?, transcript = ? WHERE message_id = ?", (raw, text, message_id)
            )
            self._append(message_id, "transcribed", {"raw": raw, "text": text, "cleaned": cleaned}, at)
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

    # -- media and voice jobs ---------------------------------------------------------

    def media_enqueue(
        self, message_id: int, file_id: str, name: str | None, mime: str | None, size: int | None
    ) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO media(message_id, file_id, name, mime, size, status)"
                " VALUES (?, ?, ?, ?, ?, 'pending')",
                (message_id, file_id, name, mime, size),
            )

    def media_pending(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM media WHERE status = 'pending' ORDER BY message_id"))

    def media_set(
        self,
        message_id: int,
        status: str,
        *,
        sha256: str | None = None,
        error: str | None = None,
        attempt: bool = False,
    ) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE media SET status = ?, sha256 = coalesce(?, sha256), last_error = ?, attempts = attempts + ?"
                " WHERE message_id = ?",
                (status, sha256, error, 1 if attempt else 0, message_id),
            )

    def voice_enqueue(self, message_id: int, file_id: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO voice_jobs(message_id, file_id, status) VALUES (?, ?, 'pending')",
                (message_id, file_id),
            )

    def voice_pending(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM voice_jobs WHERE status = 'pending' ORDER BY message_id"))

    def voice_set(self, message_id: int, status: str, *, error: str | None = None, attempt: bool = False) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE voice_jobs SET status = ?, last_error = ?, attempts = attempts + ? WHERE message_id = ?",
                (status, error, 1 if attempt else 0, message_id),
            )

    def count_entries(self) -> int:
        """Entries a person wrote (the bot's own are not counted); for `telegrind doctor`."""
        return self.conn.execute("SELECT count(*) FROM entries WHERE author != ?", (AUTHOR_BOT,)).fetchone()[0]

    def media_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, count(*) FROM media GROUP BY status")}

    def voice_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, count(*) FROM voice_jobs GROUP BY status")}

    # -- the turn ledger and the tail -------------------------------------------------

    def turn_add(self, kind: str, at: int) -> None:
        with self._tx():
            self.conn.execute("INSERT INTO turns(at, kind) VALUES (?, ?)", (at, kind))

    def turns_since(self, kind: str, since: int) -> int:
        return self.conn.execute("SELECT count(*) FROM turns WHERE kind = ? AND at >= ?", (kind, since)).fetchone()[0]

    def unprocessed(self, processed: dict[int, int]) -> list[Entry]:
        """Entries a pass would process (REQ-CONTRACT-2), as Python judges it from `processed`."""
        rows = self.conn.execute(
            f"SELECT {', '.join(ENTRY_COLUMNS)} FROM entries WHERE author != ? ORDER BY occurred_at, message_id",  # noqa: S608
            (AUTHOR_BOT,),
        )
        return [Entry(*r) for r in rows if processed.get(r["message_id"]) != r["revision"]]

    def context_before(self, message_id: int, limit: int = 10) -> list[Entry]:
        pivot = self.entry(message_id)
        if pivot is None:
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(ENTRY_COLUMNS)} FROM entries WHERE (occurred_at, message_id) < (?, ?)"  # noqa: S608
            " ORDER BY occurred_at DESC, message_id DESC LIMIT ?",
            (pivot.occurred_at, pivot.message_id, limit),
        )
        return [Entry(*r) for r in rows][::-1]

    # -- televibe.telegram.ChainStore (REQ-LOG-8); chat_id is the person's own chat, so it is not stored

    async def chain_of(self, chat_id: int, message_id: int) -> str | None:
        row = self.conn.execute("SELECT chain_id FROM chain_links WHERE message_id = ?", (message_id,)).fetchone()
        return row["chain_id"] if row else None

    async def link(self, chat_id: int, message_id: int, chain_id: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO chain_links(message_id, chain_id) VALUES (?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET chain_id = excluded.chain_id",
                (message_id, chain_id),
            )

    async def session_of(self, chain_id: str) -> str | None:
        row = self.conn.execute("SELECT session FROM chain_sessions WHERE chain_id = ?", (chain_id,)).fetchone()
        return row["session"] if row else None

    async def save(self, chain_id: str, session: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO chain_sessions(chain_id, session) VALUES (?, ?)"
                " ON CONFLICT(chain_id) DO UPDATE SET session = excluded.session",
                (chain_id, session),
            )
