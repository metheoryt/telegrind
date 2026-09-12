"""Ingestion. Store, commit, classify, and act on the verdict.

There is no echo on the fact path: the confirmation that the bot understood
is 💔, and that reaction is also the delete affordance. What the classifier
decides is what happens next — see bot/routing.py.

The row is committed *before* the classifier runs — two transactions, with
the model call between them and inside neither. Nothing written is ever
lost, and that must not come to depend on a model call: aiogram advances
the polling offset as it dispatches, so an update lost mid-handler is never
redelivered. Do not fold the two back into one.

There is no COMMAND_LIKE filter any more. The slash rule lives in
`classify.presumed`, because two rules that can disagree about whether a
message is a command is a bug found in production.
"""

import logging

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import classify, extract, store
from telegrind.bot.handlers.receipts import (
    RECEIPT_CYCLE as RECEIPT_CYCLE,
)
from telegrind.bot.handlers.receipts import (
    RECEIPT_EMOJI,
    acknowledge,
    next_receipt,
)
from telegrind.bot.router import router
from telegrind.bot.routing import HandOver, route
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_FACT, Chat

log = logging.getLogger(__name__)

#: Set by setup_dispatcher() when the meta layer is configured. None means
#: there is nobody to hand a message to, and routing says so rather than
#: going silent.
HAND_OVER: HandOver | None = None


@router.message()
async def record(
    message: Message, chat: Chat, config: ChatConfig, session: AsyncSession, bot: Bot
) -> None:
    """Store anything the user sent, then route it.

    No filter, deliberately: a sticker, a photo or a document is a message
    the user sent, so it is stored. `message_values` already handles a
    caption and a missing text, and `classify.presumed` gives a message
    with nothing readable a `fact` verdict without spending a call.

    Written first, classified second. aiogram advances the polling offset
    as it dispatches and runs handlers fire-and-forget, so a restart or a
    hung Anthropic call inside this function loses an update that is never
    redelivered — and the classifier is the only part of it that can hang.
    """
    # The first sighting is a fact, which is what shipped before the
    # classifier existed. Committing that is the invariant; everything
    # after it is refinement.
    async with session.begin():
        row, _ = await store.upsert_message(
            session, chat, message, extractable=True, verdict=VERDICT_FACT
        )
        row.receipt_emoji = RECEIPT_EMOJI

    verdict = await classify.verdict_for(message.text or message.caption)

    # A second, short transaction, and the model call is outside both. Die
    # between them and the row is a stored fact in the extraction tail —
    # the pre-classifier behaviour, which is the correct way to degrade.
    if verdict != VERDICT_FACT:
        async with session.begin():
            row.verdict = verdict
            row.extractable = False
            # No receipt is placed until `route` runs, so clearing the
            # column here is not a promise being withdrawn — it is the
            # column catching up with a verdict that earns no receipt.
            row.receipt_emoji = None

    await route(message, row, chat, config, session, bot, hand_over=HAND_OVER)


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

        # Derived, not hardcoded: `record` classifies but this observer does
        # not, and upsert_message assigns the flag unconditionally — so
        # `True` here would turn a stored /q back into extractor input the
        # first time the user fixes a typo in their own question.
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
        # Only a fact carries a receipt, on the edit path as on the ingest
        # one. Advancing the cycle here regardless would paint ❤‍🔥 on an
        # edited question — a bubble promising that tapping it deletes the
        # facts on the message, on a message that has none. Re-classifying
        # the edit is still Task 6's; this only stops the receipt claiming
        # something that did not happen.
        emoji: str | None = None
        if verdict == VERDICT_FACT:
            emoji = RECEIPT_EMOJI if created else next_receipt(row.receipt_emoji)
        row.receipt_emoji = emoji

        if was_extracted and parses:
            report = await extract.run_for(session, chat, config, row)
            log.info("re-extracted message %s: %s fact(s)", row.id, report.facts)

    if emoji is not None:
        await acknowledge(bot, edited_message.chat.id, edited_message.message_id, emoji)
