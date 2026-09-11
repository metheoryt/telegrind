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

from telegrind import extract, store
from telegrind.bot.handlers.receipts import (
    RECEIPT_CYCLE as RECEIPT_CYCLE,
)
from telegrind.bot.handlers.receipts import (
    RECEIPT_EMOJI,
    acknowledge,
    next_receipt,
)
from telegrind.bot.router import router
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_FACT, VERDICT_SYSTEM, Chat

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
    verdict: str,
) -> None:
    async with session.begin():
        row, created = await store.upsert_message(
            session, chat, message, extractable=extractable, verdict=verdict
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
    it keeps the invariant; extractable=False and verdict=VERDICT_SYSTEM keep
    it out of the taxonomy — every slash command that reaches this handler
    is one that is not /q, which claims its own messages first.
    """
    await _store(message, chat, session, bot, extractable=False, verdict=VERDICT_SYSTEM)


@router.message(F.voice)
async def record_voice(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Log the voice note without transcribing it.

    There is no ASR. Logging it now means a later transcription pass can
    reach back over everything recorded in the meantime.
    """
    await _store(message, chat, session, bot, extractable=True, verdict=VERDICT_FACT)


@router.message()
async def record_text(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Store anything else the user sent.

    No filter, deliberately: a sticker, a photo or a document is a message
    the user sent, so it is stored. `message_values` already handles a
    caption and a missing text.
    """
    await _store(message, chat, session, bot, extractable=True, verdict=VERDICT_FACT)


@router.edited_message()
async def record_edited(
    edited_message: Message,
    chat: Chat,
    session: AsyncSession,
    config: ChatConfig,
    bot: Bot,
) -> None:
    """Overwrite the text, and re-extract if there was anything to redo.

    The check has to happen *before* upsert_message, which clears
    extracted_at by design: after it, «was this already parsed» has no
    answer left.
    """
    async with session.begin():
        previous = await store.get_message(session, chat.id, edited_message.message_id)
        was_extracted = previous is not None and previous.extracted_at is not None

        # Derived, not hardcoded: this observer has no COMMAND_LIKE ahead of
        # it, and upsert_message assigns the flag unconditionally — so `True`
        # here would turn a stored /q back into extractor input the first
        # time the user fixes a typo in their own question.
        parses = not (edited_message.text or "").startswith("/")
        # Preserved, not re-derived: reclassifying on edit is Task 6's job,
        # not this one's. Deriving a verdict from the `/` prefix here would
        # be wrong regardless — it would flip an edited /q row from
        # VERDICT_QUESTION to VERDICT_FACT/SYSTEM. A row with no previous
        # sighting keeps upsert_message's first-sighting default.
        verdict = previous.verdict if previous is not None else VERDICT_FACT
        row, created = await store.upsert_message(
            session, chat, edited_message, extractable=parses, verdict=verdict
        )
        emoji = RECEIPT_EMOJI if created else next_receipt(row.receipt_emoji)
        row.receipt_emoji = emoji

        if was_extracted and parses:
            report = await extract.run_for(session, chat, config, row)
            log.info("re-extracted message %s: %s fact(s)", row.id, report.facts)

    await acknowledge(bot, edited_message.chat.id, edited_message.message_id, emoji)
