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
from aiogram.types import Message, ReactionTypeEmoji
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.router import router
from telegrind.models import Chat

log = logging.getLogger(__name__)

#: What the bot puts on every stored message. The bubble's presence is the
#: receipt; its emoji names what tapping it does, because tapping it is the
#: delete gesture. A broken heart warns without 👎's flavour of the bot
#: disapproving of every line the user writes.
RECEIPT_EMOJI = "💔"

#: A slash-prefixed message. Stored like everything else, never extracted:
#: a batch pass must not coin a kind out of a command. It resolves to None
#: rather than raising when there is no text, so a photo falls through.
COMMAND_LIKE = F.text.startswith("/")


async def acknowledge(bot: Bot, chat_id: int, message_id: int) -> None:
    """Place the receipt reaction. Never fatal.

    The message row is committed before this runs, so a Telegram failure
    here costs a visual cue and nothing else. Raising would lose the
    update; the invariant is about the row, not the bubble.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=RECEIPT_EMOJI)],
        )
    except Exception:  # cosmetic, and the row is already safe
        log.warning("could not set the receipt reaction on %s", message_id)


async def _store(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    bot: Bot,
    *,
    extractable: bool,
) -> None:
    async with session.begin():
        await store.upsert_message(session, chat, message, extractable=extractable)
    await acknowledge(bot, message.chat.id, message.message_id)


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
