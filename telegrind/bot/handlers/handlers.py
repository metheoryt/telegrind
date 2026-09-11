"""Freeform ingestion.

Registration order is match order: voice, then the slash catch-all, then
the catch-all text handler. `edited_message` is a separate observer and
does not compete with them.

Extraction and projection are gone with the workbook. What is left here
is the invariant: every handler writes the message row to Postgres
unconditionally. Task 5 of the Phase 1 plan replaces the replies with a
reaction; until then the bot still says something.
"""

import logging
from collections.abc import Callable

from aiogram import F, flags
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.router import router
from telegrind.models import Chat

log = logging.getLogger(__name__)

STORED_TEXT = "Сохранила."
VOICE_PENDING_TEXT = (
    "Голосовые пока не расшифровываю, но сообщение сохранила — разберу, когда научусь."
)
UNKNOWN_COMMAND_TEXT = "Не знаю такой команды. Сообщение сохранила."


def is_marker(marker: str) -> Callable[[str | None], bool]:
    """A filter predicate for a bare one-token reply like `-` or `??`.

    `F.text` is None for a reply that carries no text — a reply to a photo,
    a sticker, a voice note — and calling .strip() on it inside a filter
    raises *during filter evaluation*, which aborts the whole update before
    any handler runs. Guard the None here, not at the call site.
    """
    return lambda text: bool(text) and text.strip() == marker


#: A slash-prefixed message that no Command filter claimed. Registered
#: immediately before the catch-all so a mistyped /rebiuld is not extracted.
COMMAND_LIKE = F.text.startswith("/")


@router.message(F.voice)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_voice(message: Message, chat: Chat, session: AsyncSession) -> None:
    """Log the voice note without transcribing it."""
    async with session.begin():
        await store.upsert_message(session, chat, message)
    await message.reply(VOICE_PENDING_TEXT)


@router.message(COMMAND_LIKE)
async def unknown_command(message: Message, chat: Chat, session: AsyncSession) -> None:
    """Store an unrecognised command instead of treating it as a fact.

    Declining to *extract* something is never licence to drop it.
    """
    async with session.begin():
        await store.upsert_message(session, chat, message)
    await message.reply(UNKNOWN_COMMAND_TEXT)


@router.message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_text(message: Message, chat: Chat, session: AsyncSession) -> None:
    async with session.begin():
        await store.upsert_message(session, chat, message)
    await message.reply(STORED_TEXT)


@router.edited_message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_edited(
    edited_message: Message, chat: Chat, session: AsyncSession
) -> None:
    async with session.begin():
        await store.upsert_message(session, chat, edited_message)
