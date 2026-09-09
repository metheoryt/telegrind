"""Message and fact repository.

The log is append-on-first-sight, overwrite-on-edit. Nothing here deletes a
message row: a Telegram delete removes facts, never the log.
"""

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
    session: AsyncSession, chat: Chat, msg: Message
) -> tuple[LoggedMessage, bool]:
    """Append the message, or overwrite it if we have seen this id before.

    Returns `(row, created)`. Message revision history is out of scope: an
    edit overwrites the text and bumps edited_at.
    """
    values = message_values(msg)
    existing = await get_message(session, chat.id, msg.message_id)
    if existing is not None:
        # Never clobber a stored transcript with None on a text edit.
        for key, value in values.items():
            if key in ("transcript", "transcript_model") and value is None:
                continue
            setattr(existing, key, value)
        return existing, False

    row = LoggedMessage(chat_pk=chat.id, **values)
    session.add(row)
    await session.flush()
    return row, True


async def facts_for_message(session: AsyncSession, message_pk: int) -> list[Fact]:
    result = await session.execute(
        select(Fact).where(Fact.message_pk == message_pk).order_by(Fact.seq)
    )
    return list(result.scalars())


async def facts_for_chat(
    session: AsyncSession, chat_pk: int, worksheet: str | None = None
) -> list[Fact]:
    query = select(Fact).where(Fact.chat_pk == chat_pk)
    if worksheet is not None:
        query = query.where(Fact.worksheet == worksheet)
    result = await session.execute(query.order_by(Fact.id))
    return list(result.scalars())


async def fact_keys_for_worksheet(
    session: AsyncSession, chat_pk: int, worksheet: str
) -> set[str]:
    """Every sheet_key this chat's facts claim in one worksheet.

    /rebuild compares this against the worksheet's real column A to find
    rows it cannot account for.
    """
    result = await session.execute(
        select(Fact.sheet_key).where(
            Fact.chat_pk == chat_pk, Fact.worksheet == worksheet
        )
    )
    return set(result.scalars())
