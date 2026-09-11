"""Ingestion. Store, mark, react — and say nothing.

There is no echo. Writing a message produces no reply: the confirmation
that the bot understood arrives when you ask, in the answer to /q. What
the bot does say on ingest is one reaction, and that reaction is also the
delete affordance — see handlers/reactions.py.

Registration order is match order: the slash catch-all first so a command
is never recorded as a fact, then voice, then everything else.
"""

import logging

from aiogram import Bot, F
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.handlers.receipts import (
    RECEIPT_CYCLE as RECEIPT_CYCLE,
)
from telegrind.bot.handlers.receipts import (
    RECEIPT_EMOJI,
    acknowledge,
    next_receipt,
)
from telegrind.bot.router import router
from telegrind.models import Chat

log = logging.getLogger(__name__)

#: A slash-prefixed message. Stored like everything else, never extracted:
#: a batch pass must not coin a kind out of a command. It resolves to None
#: rather than raising when there is no text, so a photo falls through.
COMMAND_LIKE = F.text.startswith("/")


async def _store(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    bot: Bot,
    *,
    extractable: bool,
) -> None:
    async with session.begin():
        row, created = await store.upsert_message(
            session, chat, message, extractable=extractable
        )
        # A first sighting gets the default; an edit gets the next one along,
        # and that change is the only thing telling the user the bot saw it.
        emoji = RECEIPT_EMOJI if created else next_receipt(row.receipt_emoji)
        row.receipt_emoji = emoji
    await acknowledge(bot, message.chat.id, message.message_id, emoji)


@router.message(COMMAND_LIKE)
async def record_command(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Store a command without extracting it.

    /q is answered in Phase 2 and reaches this handler until then. Storing
    it keeps the invariant; extractable=False keeps it out of the taxonomy.
    """
    await _store(message, chat, session, bot, extractable=False)


@router.message(F.voice)
async def record_voice(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Log the voice note without transcribing it.

    There is no ASR. Logging it now means a later transcription pass can
    reach back over everything recorded in the meantime.
    """
    await _store(message, chat, session, bot, extractable=True)


@router.message()
async def record_text(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Store anything else the user sent.

    No filter, deliberately: a sticker, a photo or a document is a message
    the user sent, so it is stored. `message_values` already handles a
    caption and a missing text.
    """
    await _store(message, chat, session, bot, extractable=True)


@router.edited_message()
async def record_edited(
    edited_message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """An edit overwrites the text and clears the extraction state.

    upsert_message does the clearing, so the next batch pass picks the
    message up again. Nothing is re-extracted here: extraction happens
    when you ask.
    """
    await _store(edited_message, chat, session, bot, extractable=True)
