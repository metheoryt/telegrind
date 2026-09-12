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

#: What the user is told when an arm blew up. Fixed, markup-free and ours,
#: so it goes out with the bot-wide HTML default like every other sentence
#: the bot wrote itself — an exception string interpolated in here would
#: carry a `<` sooner or later and lose the apology to «can't parse
#: entities», which is the failure this notice exists to report. The detail
#: goes to the log, where it can be read.
BROKEN = "Что-то сломалось, попробуй ещё раз."

#: A hard Bot API limit on one message. Over it `send_message` is a 400,
#: which the guard below would turn into BROKEN — losing an answer already
#: paid for with two model calls. `meta_wiring.speak` caps its side for the
#: same reason and says more about why splitting is not the answer.
TELEGRAM_LIMIT = 4096


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
    """Act on a verdict, and never let a failure be silent.

    The row is already committed before this runs, so nothing written is at
    risk here — but *everything else* is. aiogram advances the polling
    offset as it dispatches, so an exception escaping this function is an
    update that is never redelivered: no answer, no reaction, no second
    chance. `acknowledge` swallows, so the fact arm cannot go quiet; every
    other step can. `store.unextracted_tail`, both model calls inside
    `answer_for`, every `say`, and `hand_over`'s own read and write are all
    one 429 away from it, and a 429 or a 529 from Anthropic is the
    commonest failure this bot will ever see.

    So the arms are guarded, and the failure is said out loud. What the row
    looks like afterwards is deliberate: **nothing is written here**. The
    receipt stays as `record` left it — bare for a question or for talk —
    because no emoji in the vocabulary means «this went wrong», and a bare
    `receipt_emoji` is exactly what keeps the message recoverable:
    `record_edited`'s point-of-no-return gate reads `== HANDED_OVER`, so an
    edit re-classifies and re-routes it, and re-asking always works. The
    bare bubble on its own would claim «queued» about something that is
    not; the sentence in the chat is what corrects that claim.
    """
    try:
        await _act(
            message,
            row,
            chat,
            config,
            session,
            bot,
            hand_over=hand_over,
            receipt=receipt,
        )
    except Exception:
        # Not BaseException: CancelledError is the shutdown path, and a
        # message apologising for being shut down is noise. The reason is
        # a plain comment because a BLE001 suppression is itself an error
        # here — BLE is not an enabled rule set, so RUF100 calls it unused.
        log.exception("routing message %s blew up", message.message_id)
        try:
            await say(bot, session, chat, BROKEN, reply_to=message.message_id)
        except Exception:
            # The outage that broke the arm can break the apology, and a
            # guard that raises is not a guard.
            log.exception("could not even say so about %s", message.message_id)


async def _act(
    message: Message,
    row: LoggedMessage,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
    *,
    hand_over: HandOver | None,
    receipt: str,
) -> None:
    """The four arms. Split out only so `route` can be one `try`: a guard
    per arm would be three copies that drift, and a fifth arm added later
    would arrive unguarded."""
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
            # markup-free, so they keep the default. `answer_for` can also
            # return our own EMPTY_QUESTION down this branch, which is
            # markup-free too and simply comes along — the rule is that a
            # site which *can* carry model-authored text overrides, the
            # same rule `meta_wiring.speak` states for Claude's side.
            await say(
                bot,
                session,
                chat,
                text[:TELEGRAM_LIMIT],
                reply_to=message.message_id,
                parse_mode=None,
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
