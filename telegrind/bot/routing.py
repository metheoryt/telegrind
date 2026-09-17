"""The verdict decides: a receipt, an answer, or nothing at all.

The one place the arms meet. There used to be a third kind of arm — a
`hand_over` callable that took a `talk` message to Claude Code running as a
handler in this very process. It went on 2026-09-16 with the last of the
meta layer: a process cannot rebuild and restart itself, so Claude lives in
its own bot on the host now (`~/my/cladaeb`) and nothing will ever attach
here again. The seam was kept for two days on the theory that an in-process
answerer might come back; it will not, and an untaken seam threaded through
two signatures and a dozen tests costs more than it reserves.

The receipt is the routing signal. 💔 means «understood as a fact, will
extract it», and tapping it deletes. Everything that is not a fact is
stored and left bare.
"""

import logging

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.answering import REFUSAL, answer_for, question_of
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI, acknowledge
from telegrind.bot.outbound import say
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, Chat, Entry

log = logging.getLogger(__name__)

#: What the user is told when an arm blew up. Fixed, markup-free and ours,
#: so it goes out with the bot-wide HTML default like every other sentence
#: the bot wrote itself — an exception string interpolated in here would
#: carry a `<` sooner or later and lose the apology to «can't parse
#: entities», which is the failure this notice exists to report. The detail
#: goes to the log, where it can be read.
BROKEN = "Что-то сломалось, попробуй ещё раз."

#: A hard Bot API limit on one message. Over it `send_message` is a 400,
#: which the guard below would turn into BROKEN — losing an answer already
#: paid for with two model calls. Cap rather than split: half an answer
#: delivered as two messages reads worse than one that says it was cut.
TELEGRAM_LIMIT = 4096


async def route(
    message: Message,
    entry: Entry,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
    *,
    receipt: str = RECEIPT_EMOJI,
) -> None:
    """Act on a verdict, and never let a failure be silent.

    The row is already committed before this runs, so nothing written is at
    risk here — the message and its entry both, which is what lets this
    read a verdict without a second lookup — but *everything else* is.
    aiogram advances the polling offset as it dispatches, so an exception
    escaping this function is an update that is never redelivered: no
    answer, no reaction, no second chance. `acknowledge` swallows, so the
    fact arm cannot go quiet; the question arm can. `store.unextracted_tail`,
    both model calls inside `answer_for` and every `say` are one 429 away
    from it, and a 429 or a 529 from Anthropic is the commonest failure
    this bot will ever see.

    So the arms are guarded, and the failure is said out loud. What the row
    looks like afterwards is deliberate: **nothing is written here**. The
    receipt stays as `record` left it — bare for a question or for talk —
    because no emoji in the vocabulary means «this went wrong», and a bare
    `receipt_emoji` is exactly what keeps the message recoverable: an edit
    re-classifies and re-routes it, and re-asking always works. The bare
    bubble on its own would claim «queued» about something that is not; the
    sentence in the chat is what corrects that claim.
    """
    try:
        await _act(message, entry, chat, config, session, bot, receipt=receipt)
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
    entry: Entry,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
    *,
    receipt: str,
) -> None:
    """The arms. Split out only so `route` can be one `try`: a guard per
    arm would be copies that drift, and an arm added later would arrive
    unguarded.

    `talk` and `system` share the fall-through, and that is not two verdicts
    collapsing into one. What `talk` buys is staying out of `unextracted_tail`,
    which selects on `fact` — it earns no receipt because 💔 promises a delete
    gesture over facts a talk message has none of, and it earns no reply
    because there is nobody in this process to write one.
    """
    if entry.verdict == VERDICT_FACT:
        await acknowledge(bot, chat.chat_id, message.message_id, receipt)
        return

    if entry.verdict == VERDICT_QUESTION:
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
            # site which *can* carry model-authored text overrides.
            await say(
                bot,
                session,
                chat,
                text[:TELEGRAM_LIMIT],
                reply_to=message.message_id,
                parse_mode=None,
            )
            return
        # `answer.spec_for` could not express it. That used to be the
        # hand-off to Claude, which is what let the classifier's fact/question
        # boundary be soft — a misroute cost a second of latency instead of
        # an unanswered question. With nobody to hand to, the refusal is the
        # whole of it, and the boundary is that much less forgiving.
        await say(bot, session, chat, REFUSAL, reply_to=message.message_id)
        return

    # VERDICT_TALK and VERDICT_SYSTEM: stored, and nothing else.
