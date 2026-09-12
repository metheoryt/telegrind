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
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot import outbound
from telegrind.bot.handlers.receipts import HANDED_OVER, acknowledge, clear_receipt
from telegrind.models import VERDICT_SYSTEM, Chat, LoggedMessage


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
    #
    # And the Chat read here is handed to `say`, which reads `chat.chat_id`
    # after this transaction has committed and before its own opens — so
    # this is the second consumer of `main.py`'s `expire_on_commit=False`,
    # on a path no fake session can reach. It works because `_mark` and
    # `_deliver` draw from that sessionmaker. `main.py` says so too; if one
    # of the two moves, move both.
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


async def release_hand_overs(
    session: AsyncSession, admin_chat_ids: frozenset[int]
) -> list[int]:
    """Take the marker off rows no live worker can possibly own.

    `claim` writes the point of no return before `submit`, deliberately —
    and nothing but `set_receipt(started=False)`, inside a **live** worker,
    ever takes it off. A Ctrl-C, an OOM kill or a deploy restart therefore
    leaves the marker on a turn that will never answer: aiogram advanced
    the polling offset as it dispatched, so the update is not redelivered,
    and `record_edited`'s gate then refuses to re-classify or re-route that
    message for the life of the *database*, not of the process. The only
    recovery was «send a new message», and nothing on screen said so. This
    runs once at boot, before polling, which is the one moment at which
    «no worker can own this» is knowable: the process that could have is
    gone.

    **The predicate.** A row is released when it still carries the marker
    and **no message the bot itself stored sits later in the same chat**.
    Three choices in that sentence, each load-bearing:

    *Later by `id`*, the serial primary key, because that is the only
    monotonic ordering here. `tg_date` is not — a forward is dated by its
    origin — and `message_id` is Telegram's, on a message we may never have
    stored. `outbound.say` stores the answer after the message it answers,
    so a larger `id` on a bot row is proof that a turn ran to the end.

    *«A message the bot stored»* rather than «the reply to this message».
    The reply linkage would be exact, and it is read out of
    `raw['reply_to_message']` — a field of a Telegram *response* object
    that no test has ever seen (the dev walk's item 1 is what retires it).
    Any bot row is strictly more protective: a reply to M is itself stored
    after M, so «nothing after it» implies «no reply to it», and the
    conservative half is the half that must not be wrong.

    *Two clauses for «the bot's own»*, joined by OR for the same reason.
    `raw['from_user']['is_bot']` is what `extract.author_of` reads — the
    key is `from_user`, not the Bot API's `from`, because `upsert_message`
    dumps with `model_dump(mode="json")` and no `by_alias=True` — and
    `verdict == VERDICT_SYSTEM` is what `outbound.say` writes on every one
    of those rows. Either alone would do; together, a row missing the JSON
    key still shields. `system` also covers the user's own non-`/q`
    commands, which only ever over-protects.

    So it errs towards leaving a stranded message stranded — today's
    behaviour — and away from releasing one that was answered, which would
    let the next edit hand the same message over a second time.

    Scoped to the allowlist, because the layer cleans up after itself and
    after nothing else. The narrowing is SQL; the decision is Python, and
    it re-states the marker test rather than trusting the WHERE clause —
    a fake session cannot evaluate SQL, so a decision left in the query
    would be a decision no test can see. The price of keeping the predicate
    in one place is that the candidate read is every message ever handed to
    Claude in those chats: a successful turn keeps 👀 for good, so the set
    only grows. It is one read, once, at boot, on a single-user bot. If it
    ever stops being cheap, narrow it with `load_only` — do not move the
    decision into the query.

    One way it can still be wrong in the dangerous direction, and it takes
    two failures in one outage: `outbound.say` sends before it stores, so
    an answer that reached Telegram and then lost its row leaves no bot row
    to shield the message — and if the same outage also defeats the
    worker's `_mark_all(batch, False)`, the marker survives with nothing
    after it. The sweep then releases a message that *was* answered, and an
    edit can produce a second turn. One duplicate answer is the whole harm.
    """
    if not admin_chat_ids:
        return []

    released: list[int] = []
    # One transaction for both reads and the writes. A bare read autobegins
    # one that never closes, and this session comes from the sessionmaker
    # the whole bot draws from.
    async with session.begin():
        result = await session.execute(
            select(LoggedMessage)
            .join(Chat, Chat.id == LoggedMessage.chat_pk)
            .where(
                LoggedMessage.receipt_emoji == HANDED_OVER,
                Chat.chat_id.in_(admin_chat_ids),
            )
        )
        candidates = list(result.scalars())
        if not candidates:
            return []

        spoken = await session.execute(
            select(LoggedMessage.chat_pk, func.max(LoggedMessage.id))
            .where(
                LoggedMessage.chat_pk.in_({row.chat_pk for row in candidates}),
                or_(
                    LoggedMessage.raw["from_user"]["is_bot"].astext == "true",
                    LoggedMessage.verdict == VERDICT_SYSTEM,
                ),
            )
            .group_by(LoggedMessage.chat_pk)
        )
        # `.tuples()` is a typing narrowing and nothing else — the same
        # two-column rows, typed as the tuples they are, because `dict()`
        # over a bare `Row` is a `ty` error and the comprehension that
        # avoids it is a ruff one (C416). The two gates disagree; this
        # satisfies both.
        last_spoken: dict[int, int] = dict(spoken.tuples().all())

        for row in candidates:
            if row.receipt_emoji != HANDED_OVER:
                continue
            if last_spoken.get(row.chat_pk, 0) > row.id:
                continue
            row.receipt_emoji = None
            released.append(row.message_id)

    return released
