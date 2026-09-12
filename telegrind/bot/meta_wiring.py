"""Everything the meta layer needs from telegrind, and nothing more.

`MetaLayer.__init__` takes six things, and the host supplies all six: a
`MetaConfig`, which `bot/setup.py` builds from the environment; a
sessionmaker, because a worker outlives the update that queued it; and four
callables — which are this file, all of it. Only five of the six are
*coupling*, as `telegrind/meta/__init__.py` counts it: `MetaConfig` is that
package's own type and names nothing of ours. Either way this file exists so
that separating the conversation half out later is deleting one file rather
than unpicking a merge — which is the whole reason Claude is a handler and
not a poller.

Every function here reads inside its own `session.begin()`. A bare read
autobegins a transaction that never closes, and the next `session.begin()`
then raises «a transaction is already begun» — the trap `bot/routing.py`'s
question arm and `handlers.record_edited` already document at the site, and
the one the fake sessions in the suite structurally cannot reproduce.
"""

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot import outbound
from telegrind.bot.handlers.receipts import HANDED_OVER, acknowledge, clear_receipt
from telegrind.models import Chat


async def parent_of(session: AsyncSession, chat_pk: int, message_id: int) -> int | None:
    """What message N replied to, read off our own row.

    Telegram does not nest replies, so this is the only place the second
    hop of a session lookup can come from — and it is why the bot storing
    what it says had to land first.
    """
    async with session.begin():
        row = await store.get_message(session, chat_pk, message_id)
        return store.reply_to(row) if row is not None else None


async def claim(session: AsyncSession, chat_pk: int, message_id: int) -> None:
    """Record the hand-over on the row, before the queue is told about it.

    The bubble and the column are two different promises and they are made
    at two different moments. 👀 *on screen* goes on when the turn starts,
    so the queue stays legible: the messages still bare are the ones not
    yet seen. 👀 *in the column* is the point of no return, and it has to
    be true the instant `hand_over` returns — `record` is finished by then,
    and an edit arriving next reads `receipt_emoji == HANDED_OVER` to
    decide whether this message was already given away.

    Without this the window is not milliseconds. A message queued behind a
    running turn on the same key is not marked until that turn ends, so an
    edit minutes later would find a bare row, re-classify, and hand the
    same message over a second time.
    """
    async with session.begin():
        row = await store.get_message(session, chat_pk, message_id)
        if row is not None:
            row.receipt_emoji = HANDED_OVER


async def set_receipt(
    bot: Bot,
    session: AsyncSession,
    chat_id: int,
    chat_pk: int,
    message_id: int,
    started: bool,
) -> None:
    """👀 on when the turn starts, off when it failed.

    The row remembers it because there is no API to read a message's
    reactions back — and because `receipt_emoji == HANDED_OVER` is what
    makes the point of no return on an edit checkable without a second
    column. Writing it again on `started` is deliberate and cheap:
    `claim` already did, but a retried or requeued turn must not depend on
    that having happened.
    """
    emoji = HANDED_OVER if started else None
    async with session.begin():
        row = await store.get_message(session, chat_pk, message_id)
        if row is not None:
            row.receipt_emoji = emoji

    if emoji is not None:
        await acknowledge(bot, chat_id, message_id, emoji)
    else:
        await clear_receipt(bot, chat_id, message_id)


async def speak(
    bot: Bot, session: AsyncSession, chat_pk: int, text: str, reply_to: int
) -> None:
    """Claude always sends with reply parameters, and never as HTML.

    Without the reply parameters a reply to Claude has no parent row to
    point at, and every second turn would start a new conversation instead
    of continuing this one.

    `parse_mode=None` is not tidiness. The bot's default is HTML, and this
    is the first code in the project that sends text no human wrote: an
    answer about code carries `<`, `>` and `&`, none of which are tags, and
    Telegram rejects the whole message with «can't parse entities» rather
    than sending it plain. Losing the answer to make one angle bracket bold
    is the wrong trade.
    """
    # In its own transaction: a bare get autobegins, and `say` opens one.
    async with session.begin():
        chat = await session.get(Chat, chat_pk)
    if chat is None:  # the row is created by the middleware before any turn
        return
    # 4096 characters is a hard Bot API limit and splitting is not the
    # answer — a long reply that does not fit a conversation leaves the
    # chat as an object. Until that exists, the truncation is visible.
    await outbound.say(
        bot, session, chat, text[:4096], reply_to=reply_to, parse_mode=None
    )
