"""Message and fact repository.

The log is append-on-first-sight, overwrite-on-edit. Nothing here deletes a
message row: a Telegram delete removes facts, never the log.
"""

from datetime import datetime
from typing import Any

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import KIND_TEXT, KIND_VOICE, Chat, Fact, LoggedMessage


def message_kind(msg: Message) -> str:
    return KIND_VOICE if getattr(msg, "voice", None) else KIND_TEXT


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
        "edited_at": getattr(msg, "edit_date", None),
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
