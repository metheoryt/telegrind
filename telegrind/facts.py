"""Read-only reads of the agent's facts.sqlite: the three tables Python depends on (SPEC REQ-FACTS-2, REQ-SCOPE-2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class FactsUnavailable(Exception):  # noqa: N818 - the name says what a caller must handle
    """facts.sqlite exists but cannot be read now (a hot journal, a lock): skip the decision, never act on {}."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Facts:
    def __init__(self, path: Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout = timeout

    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=self.timeout)

    def _rows(self, sql: str) -> list[tuple]:
        """Rows, or [] when the file or the table is not there yet. Anything else is FactsUnavailable: a hot
        journal or a lock must not read as an empty table, or a caller acts on nothing (a needless pass,
        no keyterms, the default zone)."""
        if not self.exists():
            return []
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise FactsUnavailable(type(exc).__name__) from exc
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError as exc:
            if str(exc).startswith("no such table"):  # the agent has not made it yet
                return []
            raise FactsUnavailable(type(exc).__name__) from exc
        except sqlite3.Error as exc:
            raise FactsUnavailable(type(exc).__name__) from exc
        finally:
            conn.close()

    def processed(self) -> dict[int, int]:
        rows = self._rows("SELECT entry_id, revision FROM processed")
        return {int(entry_id): int(revision) for entry_id, revision in rows}

    def mark(self) -> tuple[int, int]:
        """(row count, greatest revision) of `processed`: what REQ-TURN-7 compares before and after a pass."""
        rows = self._rows("SELECT count(*), coalesce(max(revision), 0) FROM processed")
        return (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)

    def keyterms(self) -> list[str]:
        return [str(term) for (term,) in self._rows("SELECT term FROM keyterms ORDER BY term")]

    def tz(self) -> str | None:
        rows = self._rows("SELECT value FROM settings WHERE key = 'tz'")
        return str(rows[0][0]) if rows else None
