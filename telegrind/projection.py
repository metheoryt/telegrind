"""Facts -> worksheet rows. One-way, by design.

Row indices are never stored: delete_rows shifts them. A fact stores its
worksheet and its sheet_key, so a single-row operation is one DB read plus
one find() in one known sheet.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from telegrind.llm import RawFact
from telegrind.models import Fact
from telegrind.registry import Category

log = logging.getLogger(__name__)


def sheet_key(message_id: int, seq: int) -> str:
    """What goes in column A. Uniform, including for single-fact messages —
    a message can gain a second fact on a later edit.

    The separator is `_` and not `.` because column A is written with
    ValueInputOption.user_entered: Sheets would parse a dotted "4821.10" as
    the number 4821.1, render it back as "4821.1", and collide it with seq 1.
    An underscore is text in every locale, so the key round-trips exactly.
    """
    return f"{message_id}_{seq}"


def fact_row(cat: Category, key: str, fields: dict[str, object]) -> list[object]:
    """The key, then the category's declared columns in order."""
    return [key, *(fields.get(c.header, "") for c in cat.columns)]


class ChangeKind(StrEnum):
    REWRITE = "rewrite"
    MOVE = "move"
    DELETE = "delete"
    APPEND = "append"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    seq: int
    #: The stored fact, for REWRITE / MOVE / DELETE.
    fact: Fact | None = None
    #: The newly extracted fact, for REWRITE / MOVE / APPEND.
    raw: RawFact | None = None


def diff_facts(old: list[Fact], new: list[RawFact]) -> list[Change]:
    """Diff re-extracted facts against stored ones, by seq.

    A MOVE is the case today's code cannot express: change_row rewrites in
    place, so editing "4500 такси" into "вес 82.4" needs the row deleted
    from Expenses and appended to Telemetry.

    A same-category match is always a REWRITE, never a no-op. Rewriting is
    idempotent and one API call; comparing fields to decide would be a
    second source of truth about equality.
    """
    by_seq = {f.seq: f for f in old}
    changes: list[Change] = []

    for seq, raw in enumerate(new, start=1):
        stored = by_seq.pop(seq, None)
        if stored is None:
            changes.append(Change(ChangeKind.APPEND, seq, raw=raw))
        elif stored.category == raw.category:
            changes.append(Change(ChangeKind.REWRITE, seq, fact=stored, raw=raw))
        else:
            changes.append(Change(ChangeKind.MOVE, seq, fact=stored, raw=raw))

    changes.extend(
        Change(ChangeKind.DELETE, seq, fact=by_seq[seq]) for seq in sorted(by_seq)
    )

    return sorted(changes, key=lambda c: c.seq)
