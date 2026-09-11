"""Message and fact repository.

The log is append-on-first-sight, overwrite-on-edit. Nothing here deletes a
message row: a Telegram delete removes facts, never the log.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram.types import Message
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import KIND_TEXT, KIND_VOICE, Chat, Fact, LoggedMessage

if TYPE_CHECKING:  # `extract` imports `store`; the cycle stays a type concern.
    from telegrind.extract import Draft


def message_kind(msg: Message) -> str:
    return KIND_VOICE if getattr(msg, "voice", None) else KIND_TEXT


def edited_at(msg: Message) -> datetime | None:
    """The edit timestamp, as a datetime.

    aiogram parses `date` into a datetime but leaves `edit_date` a raw
    Unix int, and the column is a timestamptz — so handing it straight
    over kills the whole edit with a DataError. Telegram's timestamps are
    UTC.
    """
    raw = getattr(msg, "edit_date", None)
    if raw is None or isinstance(raw, datetime):
        return raw
    return datetime.fromtimestamp(raw, tz=UTC)


def message_values(msg: Message) -> dict[str, Any]:
    """Lift the log columns off an aiogram message.

    A forwarded message's own date is the date of the forwarded content,
    which is what `services/expense.py` used too — a forward of last week's
    receipt should not be dated today.
    """
    kind = message_kind(msg)
    voice = getattr(msg, "voice", None)
    origin = getattr(msg, "forward_origin", None)

    return {
        "message_id": msg.message_id,
        "kind": kind,
        "text": None if kind == KIND_VOICE else (msg.text or msg.caption),
        "transcript": None,
        "transcript_model": None,
        "audio_file_id": voice.file_id if voice else None,
        "audio_duration": voice.duration if voice else None,
        "tg_date": origin.date if origin else msg.date,
        "edited_at": edited_at(msg),
        "raw": msg.model_dump(mode="json"),
    }


async def get_message(
    session: AsyncSession, chat_pk: int, message_id: int
) -> LoggedMessage | None:
    result = await session.execute(
        select(LoggedMessage).where(
            LoggedMessage.chat_pk == chat_pk,
            LoggedMessage.message_id == message_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_message(
    session: AsyncSession,
    chat: Chat,
    msg: Message,
    *,
    extractable: bool = True,
) -> tuple[LoggedMessage, bool]:
    """Append the message, or overwrite it if we have seen this id before.

    Returns `(row, created)`. An edit overwrites the text, bumps edited_at,
    and clears the extraction state: the text changed, so whatever was
    extracted from it no longer describes the message, and clearing
    extracted_at is what makes the next batch pass pick it up again.
    """
    values = message_values(msg)
    existing = await get_message(session, chat.id, msg.message_id)
    if existing is not None:
        # Never clobber a stored transcript with None on a text edit.
        for key, value in values.items():
            if key in ("transcript", "transcript_model") and value is None:
                continue
            setattr(existing, key, value)
        existing.extractable = extractable
        existing.extracted_at = None
        existing.extract_error = None
        return existing, False

    row = LoggedMessage(chat_pk=chat.id, extractable=extractable, **values)
    session.add(row)
    await session.flush()
    return row, True


def _has_content() -> Any:
    """SQL for «this message has something the extractor can read».

    A photo, a sticker or a location is stored like everything else and
    simply waits: not yet parsed, which is neither «yielded nothing» nor
    «failed». When something can read a photo, the rows are still here.
    """
    return func.coalesce(
        func.nullif(func.trim(LoggedMessage.transcript), ""),
        func.nullif(func.trim(LoggedMessage.text), ""),
    ).is_not(None)


async def unextracted_tail(
    session: AsyncSession, chat_pk: int, *, limit: int = 200
) -> list[LoggedMessage]:
    """The messages a pass is responsible for, oldest first."""
    result = await session.execute(
        select(LoggedMessage)
        .where(
            LoggedMessage.chat_pk == chat_pk,
            LoggedMessage.extractable.is_(True),
            LoggedMessage.extracted_at.is_(None),
            _has_content(),
        )
        .order_by(LoggedMessage.tg_date, LoggedMessage.message_id)
        .limit(limit)
    )
    return list(result.scalars())


async def context_before(
    session: AsyncSession,
    chat_pk: int,
    pivot: LoggedMessage,
    *,
    limit: int = 10,
) -> list[LoggedMessage]:
    """Read-only neighbours shown to the model but never re-extracted.

    Ordered by chat time, not by `id`: a forward is dated by its origin,
    so the two genuinely differ. The comparison is a row comparison so
    that two messages sharing a second still order deterministically.
    """
    result = await session.execute(
        select(LoggedMessage)
        .where(
            LoggedMessage.chat_pk == chat_pk,
            _has_content(),
            tuple_(LoggedMessage.tg_date, LoggedMessage.message_id)
            < (pivot.tg_date, pivot.message_id),
        )
        .order_by(LoggedMessage.tg_date.desc(), LoggedMessage.message_id.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def facts_for_message(session: AsyncSession, message_pk: int) -> list[Fact]:
    result = await session.execute(
        select(Fact).where(Fact.message_pk == message_pk).order_by(Fact.seq)
    )
    return list(result.scalars())


async def facts_for_chat(session: AsyncSession, chat_pk: int) -> list[Fact]:
    result = await session.execute(
        select(Fact).where(Fact.chat_pk == chat_pk).order_by(Fact.id)
    )
    return list(result.scalars())


async def live_facts_for_message(session: AsyncSession, message_pk: int) -> list[Fact]:
    """This message's facts that are not tombstoned."""
    result = await session.execute(
        select(Fact)
        .where(Fact.message_pk == message_pk, Fact.deleted_at.is_(None))
        .order_by(Fact.seq)
    )
    return list(result.scalars())


async def tombstone_facts(session: AsyncSession, message_pk: int, at: datetime) -> int:
    """Soft-delete this message's live facts. Returns how many were stamped.

    A tombstone is never lifted by an extraction pass — only restore_facts
    clears it — because facts are re-derivable and a hard delete would be
    undone by the next re-extraction of the same message.
    """
    result = await session.execute(
        select(Fact).where(Fact.message_pk == message_pk, Fact.deleted_at.is_(None))
    )
    rows = list(result.scalars())
    for row in rows:
        row.deleted_at = at
    return len(rows)


async def restore_facts(session: AsyncSession, message_pk: int) -> int:
    """Clear the tombstone on this message's facts. Returns how many."""
    result = await session.execute(
        select(Fact).where(Fact.message_pk == message_pk, Fact.deleted_at.is_not(None))
    )
    rows = list(result.scalars())
    for row in rows:
        row.deleted_at = None
    return len(rows)


def mark_extracted(
    rows: list[LoggedMessage], *, model: str, prompt_version: str, at: datetime
) -> None:
    """A message the pass handled, whether or not it yielded a fact.

    Marking the silent ones is the whole point of the column: without it
    «привет» is indistinguishable from «not yet parsed» and every pass
    re-feeds it forever.
    """
    for row in rows:
        row.extracted_at = at
        row.extract_model = model
        row.extract_prompt_version = prompt_version
        row.extract_error = None


def mark_failed(rows: list[LoggedMessage], error: str) -> None:
    """The pass could not read these. They stay pending and are retried.

    Dropping the echo removed the only channel through which a failure
    reached the user, so it has to be countable here instead.
    """
    for row in rows:
        row.extract_error = error


async def replace_facts(
    session: AsyncSession,
    chat_pk: int,
    message_pk: int,
    drafts: list[Draft],
    *,
    model: str,
    prompt_version: str,
    now: datetime,
) -> int:
    """Diff this message's facts against what the pass just derived.

    Unchanged rows are left alone, changed ones updated in place, surplus
    ones tombstoned. A tombstone is never lifted here — only the user's
    reaction clears `deleted_at` — which is why inserting over a
    tombstoned `(message_pk, seq)` has to work, and why the uniqueness on
    it is a partial index.
    """
    live = {row.seq: row for row in await live_facts_for_message(session, message_pk)}

    for draft in drafts:
        row = live.pop(draft.seq, None)
        if row is None:
            session.add(
                Fact(
                    chat_pk=chat_pk,
                    message_pk=message_pk,
                    seq=draft.seq,
                    kind=draft.kind,
                    at=draft.at,
                    fields=draft.fields,
                    model=model,
                    prompt_version=prompt_version,
                )
            )
            continue
        row.kind = draft.kind
        row.at = draft.at
        row.fields = draft.fields
        row.model = model
        row.prompt_version = prompt_version

    for surplus in live.values():
        surplus.deleted_at = now

    return len(drafts)
