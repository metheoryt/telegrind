"""Read-only reads of the agent's facts.sqlite: the three tables Python depends on (SPEC REQ-FACTS-2, REQ-SCOPE-2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class Facts:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)

    def _rows(self, sql: str) -> list[tuple]:
        if not self.exists():
            return []
        conn = self._connect()
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.OperationalError:  # no such table: the agent has not made it yet
            return []
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
