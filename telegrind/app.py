"""The message path: store first, then react, enqueue or ask (SPEC sections 4 and 6)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, Message, MessageReactionUpdated, ReactionTypeEmoji, ReplyParameters, User

from telegrind.config import Config
from telegrind.incoming import AUTHOR_BOT, Incoming, incoming
from telegrind.layout import Layout
from telegrind.log import Log, now
from telegrind.receipts import FIRST, next_receipt
from telegrind.triggers import BARE_Q, BARE_Q_TEXT, question_text

log = logging.getLogger(__name__)


class Answerer(Protocol):
    async def answer(self, uid: int, message: Message, text: str) -> None: ...


class JobQueue(Protocol):
    def enqueue(self, uid: int, inc: Incoming) -> None: ...


class BacklogWatcher(Protocol):
    def on_entry(self, uid: int) -> None: ...


class App:
    def __init__(
        self,
        *,
        config: Config,
        bot: Any,
        layout: Layout,
        answerer: Answerer | None = None,
        media: JobQueue | None = None,
        voice: JobQueue | None = None,
        watcher: BacklogWatcher | None = None,
        clock: Callable[[], int] = now,
    ) -> None:
        self.config, self.bot, self.layout = config, bot, layout
        self.answerer, self.media, self.voice, self.watcher, self.clock = answerer, media, voice, watcher, clock
        self.logs: dict[int, Log] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- plumbing ----------------------------------------------------------------------

    def log_for(self, uid: int) -> Log:
        if uid not in self.logs:
            self.layout.ensure_user(uid)
            self.logs[uid] = Log(self.layout.log_path(uid))
        return self.logs[uid]

    def allowed(self, chat: Chat, user: User | None) -> int | None:
        """The person's id when this is an allowed user's own private chat (REQ-ACCESS-1, REQ-ACCESS-2)."""
        if chat.type != "private" or user is None or user.id not in self.config.allowed_user_ids or chat.id != user.id:
            return None
        return user.id

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def close(self) -> None:
        for store in self.logs.values():
            store.close()
        self.logs.clear()

    async def react(self, uid: int, message_id: int, emoji: str | None) -> None:
        """Set or clear the receipt; a failure costs the cue and nothing else (REQ-RECEIPT-1)."""
        try:
            await self.bot.set_message_reaction(
                chat_id=uid,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)] if emoji else [],
            )
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            log.warning("reaction on %d/%d failed: %s", uid, message_id, type(exc).__name__)
            return
        self.log_for(uid).set_receipt(message_id, emoji)

    async def say(self, uid: int, text: str, reply_to: int | None) -> Message | None:
        """Send and store (REQ-LOG-7). Text the model may have written goes out with parse_mode=None."""
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to is not None else None
        try:
            sent = await self.bot.send_message(chat_id=uid, text=text, parse_mode=None, reply_parameters=reply)
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            log.warning("send to %d failed: %s", uid, type(exc).__name__)
            return None
        self.log_for(uid).record_bot_sent(
            sent.message_id, text, reply_to, sent.model_dump(mode="json", exclude_none=True), self.clock()
        )
        return sent

    # -- updates -----------------------------------------------------------------------

    async def on_message(self, msg: Message) -> None:
        uid = self.allowed(msg.chat, msg.from_user)
        if uid is None:
            return
        store = self.log_for(uid)
        inc = incoming(msg, now=self.clock())
        store.record_received(inc)  # REQ-LOG-1: committed before anything else
        question = question_text(inc.text, reply_to_author=store.author_of(inc.reply_to), author=inc.author)
        if question is None:
            await self.react(uid, inc.message_id, FIRST)
            if self.watcher is not None:
                self.watcher.on_entry(uid)
        if inc.media is not None and self.media is not None:
            self.media.enqueue(uid, inc)
        if inc.voice is not None and self.voice is not None:
            self.voice.enqueue(uid, inc)
        if question is BARE_Q:
            await self.say(uid, BARE_Q_TEXT, inc.message_id)
        elif isinstance(question, str) and self.answerer is not None:
            self.spawn(self.answerer.answer(uid, msg, question))

    async def on_edit(self, msg: Message) -> None:
        uid = self.allowed(msg.chat, msg.from_user)
        if uid is None:
            return
        store = self.log_for(uid)
        before = store.entry(msg.message_id)
        entry = store.record_edit(incoming(msg, now=self.clock()))
        if entry is None or before is None:
            return
        if before.receipt is not None:  # REQ-RECEIPT-2: only a receipted message moves its heart
            await self.react(uid, entry.message_id, next_receipt(before.receipt))
        if self.watcher is not None:
            self.watcher.on_entry(uid)

    async def on_reaction(self, update: MessageReactionUpdated) -> None:
        uid = self.allowed(update.chat, update.user)
        if uid is None:
            return
        store = self.log_for(uid)
        if store.author_of(update.message_id) in (None, AUTHOR_BOT):
            return  # unknown, or the bot's own message (REQ-REACT-1)
        payload = update.model_dump(mode="json", exclude_none=True)
        entry = store.record_reaction(
            update.message_id, deleted=bool(update.new_reaction), payload=payload, at=self.clock()
        )
        if entry is not None and self.watcher is not None:
            self.watcher.on_entry(uid)
