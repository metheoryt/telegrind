"""Facts -> worksheet rows. One-way, by design.

Row indices are never stored: delete_rows shifts them. A fact stores its
worksheet and its sheet_key, so a single-row operation is one DB read plus
one find() in one known sheet.
"""

import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from enum import StrEnum

from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.coerce import coerce_fields
from telegrind.llm import RawFact
from telegrind.models import ORIGIN_EXTRACTED, Chat, Fact, LoggedMessage
from telegrind.registry import Category, Registry
from telegrind.sheets import Config, Worksheet
from telegrind.store import fact_keys_for_worksheet, facts_for_chat

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


def _worksheet(ags: AsyncioGspreadSpreadsheet, cat: Category) -> Worksheet:
    return Worksheet(ags, cat.worksheet, cat.headers)


def unaccounted_keys(sheet_keys: set[str], fact_keys: set[str]) -> set[str]:
    """Keys present in a worksheet that no fact claims.

    A projection that can silently discard its own source is not worth the
    convenience, so /rebuild refuses when this is non-empty.
    """
    return sheet_keys - fact_keys


async def apply_changes(
    ags: AsyncioGspreadSpreadsheet,
    session: AsyncSession,
    registry: Registry,
    cfg: Config,
    chat: Chat,
    msg_row: LoggedMessage,
    changes: list[Change],
    *,
    model: str,
    prompt_version: str,
) -> list[Fact]:
    """Write one message's diff to Postgres and to the workbook.

    Postgres first, then Sheets: a fact recorded but not yet projected is
    recoverable with /rebuild, and a row written with no fact behind it is
    exactly the unaccounted state /rebuild refuses over.
    """
    written: list[Fact] = []

    for change in changes:
        if change.kind is ChangeKind.DELETE and change.fact is not None:
            await delete_facts(ags, session, registry, [change.fact])
            continue

        assert change.raw is not None
        cat = registry.by_name(change.raw.category)
        if cat is None:
            log.warning("no category %r in the registry, skipping", change.raw.category)
            continue

        key = sheet_key(msg_row.message_id, change.seq)
        fields = coerce_fields(
            cat, change.raw.fields, cfg, cfg.localized(msg_row.tg_date)
        )
        row = fact_row(cat, key, fields)
        ws = _worksheet(ags, cat)

        kind = change.kind
        if kind is ChangeKind.MOVE and change.fact is not None:
            # The row has to leave its old worksheet; there is no in-place
            # edit that can move it. Then it appends to the new one.
            await delete_facts(ags, session, registry, [change.fact])
            kind = ChangeKind.APPEND

        if kind is ChangeKind.REWRITE and change.fact is not None:
            fact = change.fact
            fact.category = cat.name
            fact.fields = fields
            fact.worksheet = cat.worksheet
            fact.sheet_key = key
            fact.model = model
            fact.prompt_version = prompt_version
            fact.extracted_at = datetime.now(tz=cfg.tz)
            row_no = await ws.find_key(key)
            if row_no is None:
                await ws.append([row])
            else:
                await ws.update_row(row_no, row)
        else:
            fact = Fact(
                chat_pk=chat.id,
                message_pk=msg_row.id,
                seq=change.seq,
                category=cat.name,
                fields=fields,
                origin=ORIGIN_EXTRACTED,
                model=model,
                prompt_version=prompt_version,
                extracted_at=datetime.now(tz=cfg.tz),
                worksheet=cat.worksheet,
                sheet_key=key,
            )
            session.add(fact)
            await ws.append([row])

        await session.flush()
        written.append(fact)

    return written


async def delete_facts(
    ags: AsyncioGspreadSpreadsheet,
    session: AsyncSession,
    registry: Registry,
    facts: list[Fact],
) -> int:
    """Delete each fact's row from its own worksheet, then the fact itself.

    The message row stays: a delete does not rewrite the log.
    """
    deleted = 0
    for fact in facts:
        cat = registry.by_worksheet(fact.worksheet)
        if cat is not None:
            ws = _worksheet(ags, cat)
            row_no = await ws.find_key(fact.sheet_key)
            if row_no is not None:
                await ws.delete_row(row_no)
        else:
            log.warning(
                "fact %s points at worksheet %r which the registry no longer "
                "declares; dropping the fact and leaving the row",
                fact.id,
                fact.worksheet,
            )
        await session.delete(fact)
        deleted += 1
    await session.flush()
    return deleted


@dataclass(frozen=True, slots=True)
class RebuildReport:
    rebuilt: dict[str, int] = dataclass_field(default_factory=dict)
    refused: dict[str, int] = dataclass_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refused


async def rebuild(
    ags: AsyncioGspreadSpreadsheet,
    session: AsyncSession,
    registry: Registry,
    chat: Chat,
    *,
    force: bool = False,
) -> RebuildReport:
    """Re-project every category's worksheet from fact rows. No LLM cost.

    Refuses a worksheet holding rows no fact accounts for, unless forced.
    That is the guard that makes the projection safe on a workbook with
    years of pre-bot history in it.
    """
    rebuilt: dict[str, int] = {}
    refused: dict[str, int] = {}

    for cat in registry.categories:
        ws = _worksheet(ags, cat)
        values = await ws.all_values()
        sheet_keys: set[str] = set()
        if len(values) > 1:
            sheet_keys = {
                row[0].strip()
                for row in values[1:]
                if row and row[0] and row[0].strip()
            }

        fact_keys = await fact_keys_for_worksheet(session, chat.id, cat.worksheet)
        orphans = unaccounted_keys(sheet_keys, fact_keys)
        if orphans and not force:
            refused[cat.worksheet] = len(orphans)
            continue

        facts = await facts_for_chat(session, chat.id, cat.worksheet)
        rows = [fact_row(cat, f.sheet_key, f.fields) for f in facts]
        await ws.clear_data()
        await ws.append(rows)
        await ws.apply_filter()
        rebuilt[cat.worksheet] = len(rows)

    return RebuildReport(rebuilt, refused)
