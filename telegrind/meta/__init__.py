"""The Claude meta layer: a handler, not a second process.

It sees the update, it owns the conversation, it spawns `claude -p` and it
sends the reply. It is a guest in whatever bot registers it — it takes a
config object and callables, and reaches into none of the host's internals.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.meta.config import MetaConfig as MetaConfig
from telegrind.meta.queue import Job, Turns
from telegrind.meta.runtime import run_turn
from telegrind.meta.sessions import session_id as session_id
from telegrind.meta.sessions import turn_key


class Conversation(Protocol):
    """What the meta layer needs off the host's chat row, and no more.

    Read-only members, because a mutable protocol attribute is invariant
    and a SQLAlchemy `Mapped[int]` would not satisfy it.
    """

    @property
    def id(self) -> int: ...

    @property
    def chat_id(self) -> int: ...


#: What the host has to supply. These four callables, plus the sessionmaker
#: `MetaLayer` is built with, are the whole coupling: nothing in this package
#: imports the host's store, query or taxonomy, so separating the module later
#: is deleting one wiring file. `__init__` takes a sixth argument, the
#: `MetaConfig`, and the host builds that too — it is left out of the count
#: here because it is this package's own type and names nothing of the host's.
ParentOf = Callable[[AsyncSession, int, int], Awaitable[int | None]]
#: Record the hand-over on the row *now*, before the queue is told. Separate
#: from `set_receipt` because the column and the bubble are two different
#: promises made at two different moments — see `MetaLayer.hand_over`.
Claim = Callable[[AsyncSession, int, int], Awaitable[None]]
SetReceipt = Callable[[Bot, AsyncSession, int, int, int, bool], Awaitable[None]]
Speak = Callable[[Bot, AsyncSession, int, str, int], Awaitable[None]]


class MetaLayer:
    """The conversation half, in one object.

    The Bot instance does not exist when the dispatcher is built, so the
    queue is constructed on the first hand-off, which is the first moment
    aiogram has handed us one.
    """

    def __init__(
        self,
        cfg: MetaConfig,
        *,
        async_session: async_sessionmaker[AsyncSession],
        parent_of: ParentOf,
        claim: Claim,
        set_receipt: SetReceipt,
        speak: Speak,
    ) -> None:
        self._cfg = cfg
        self._async_session = async_session
        self._parent_of = parent_of
        self._claim = claim
        self._set_receipt = set_receipt
        self._speak = speak
        self._turns: Turns | None = None

    @property
    def turns(self) -> Turns | None:
        """The queue, once there has been something to queue. Tests drain it."""
        return self._turns

    def _queue(self, bot: Bot) -> Turns:
        if self._turns is None:
            self._turns = Turns(
                self._cfg,
                run=functools.partial(run_turn, self._cfg),
                deliver=functools.partial(self._deliver, bot),
                mark=functools.partial(self._mark, bot),
            )
        return self._turns

    async def _mark(self, bot: Bot, job: Job, started: bool) -> None:
        """A worker outlives the update that queued it, so it opens its own
        session rather than borrowing the handler's."""
        async with self._async_session() as session:
            await self._set_receipt(
                bot, session, job.chat_id, job.chat_pk, job.message_id, started
            )

    async def _deliver(self, bot: Bot, job: Job, text: str) -> None:
        async with self._async_session() as session:
            await self._speak(bot, session, job.chat_pk, text, job.message_id)

    async def hand_over(
        self,
        message: Message,
        row: object,
        chat: Conversation,
        session: AsyncSession,
        bot: Bot,
    ) -> bool:
        """Accept the message, or say plainly that we will not.

        False is not an error: it is «this chat is not on the allowlist»,
        and routing then answers the way it would if there were no meta
        layer at all. The gate is on the chat rather than on the verdict,
        so talk from a stranger is not answered.

        The order of the last three statements is load-bearing. `claim`
        commits the hand-over to the row before `submit`, and therefore
        before this returns True — because `record` is finished the moment
        it does, and an edit arriving next decides whether the message was
        already given away by reading that column. The bubble comes later,
        when the turn actually starts; the column cannot wait for it,
        because a message queued behind a running turn on the same key
        waits out that whole turn. A failed claim propagates rather than
        being swallowed: queueing a turn whose point of no return was never
        recorded is the state this ordering exists to prevent.
        """
        # `row` is part of the HandOver signature and deliberately unused:
        # the meta layer routes on the chat, never on the verdict.
        chat_id, chat_pk = chat.chat_id, chat.id
        if not self._cfg.allows(chat_id):
            return False

        key = await turn_key(
            message,
            parent_of=functools.partial(self._parent_of, session, chat_pk),
        )
        await self._claim(session, chat_pk, message.message_id)
        await self._queue(bot).submit(
            Job(
                chat_id=chat_id,
                chat_pk=chat_pk,
                message_id=message.message_id,
                text=message.text or message.caption or "",
                key=key,
            )
        )
        return True
