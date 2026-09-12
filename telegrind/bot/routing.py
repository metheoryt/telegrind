"""The verdict decides: a receipt, an answer, or a hand-off.

The one place the three arms meet, and the seam the meta layer plugs into —
`hand_over` is passed in, so nothing here imports `telegrind.meta` and
removing the meta layer is deleting one argument at the call site.

The receipt is the routing signal. 💔 means «understood as a fact, will
extract it», and tapping it deletes. 👀 means «handed to Claude», placed by
the meta layer when the turn starts rather than here, so the queue is legible
on screen: the messages still bare are the ones not yet seen.
"""

import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.answering import REFUSAL, answer_for, question_of
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI, acknowledge
from telegrind.bot.outbound import say
from telegrind.config import ChatConfig
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_TALK,
    Chat,
    LoggedMessage,
)

log = logging.getLogger(__name__)

HandOver = Callable[[Message, LoggedMessage, Chat, AsyncSession, Bot], Awaitable[bool]]


async def route(
    message: Message,
    row: LoggedMessage,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
    *,
    hand_over: HandOver | None = None,
    receipt: str = RECEIPT_EMOJI,
) -> None:
    """Act on a verdict. The row is already committed before this runs."""
    if row.verdict == VERDICT_FACT:
        await acknowledge(bot, chat.chat_id, message.message_id, receipt)
        return

    if row.verdict == VERDICT_TALK:
        if hand_over is None or not await hand_over(message, row, chat, session, bot):
            log.info("nobody to hand message %s to", message.message_id)
        return

    if row.verdict == VERDICT_QUESTION:
        # Said in a transaction of its own, and outside the answering one:
        # the first question after a quiet week pays for the week, and
        # holding a write transaction open across two model calls to
        # announce that is the wrong shape even at one user. A bare read
        # would autobegin and make the begin() below raise «a transaction
        # is already begun».
        async with session.begin():
            pending = len(await store.unextracted_tail(session, chat.id))
        if pending:
            await say(bot, session, chat, f"Разбираю {pending} сообщений…")

        async with session.begin():
            # `text or caption`, the same expression the classifier read:
            # a photo captioned «сколько я потратил на это» is classified
            # from its caption and has no `.text`, so reading only `.text`
            # here would answer an empty question — «Спроси что-нибудь
            # после /q.» in reply to a message containing no /q. Whatever
            # decides the verdict and whatever answers it must read the
            # same words.
            body = message.text or message.caption
            text = await answer_for(question_of(body), chat, config, session)
        if text is not None:
            # `parse_mode=None`, and only here. `answer.render` writes this
            # sentence, and the bot's default is HTML: an answer carrying a
            # bare `<` — a comparison, a currency rendering — comes back as
            # «can't parse entities» and the whole answer is lost. The
            # pending notice above and REFUSAL below are ours, fixed and
            # markup-free, so they keep the default; the override belongs
            # to model-authored text, the same rule `meta_wiring.speak`
            # states for Claude's side.
            await say(
                bot, session, chat, text, reply_to=message.message_id, parse_mode=None
            )
            return
        # The bot could not express it, so Claude does. A misroute across
        # the fact/question line then costs a second of latency instead of
        # an unanswered question — which is what lets the classifier's
        # boundary be soft.
        if hand_over is not None and await hand_over(message, row, chat, session, bot):
            return
        await say(bot, session, chat, REFUSAL, reply_to=message.message_id)
        return

    # VERDICT_SYSTEM: a command that is not /q, or the bot's own message.
    # Stored, and nothing else.
