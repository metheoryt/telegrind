"""The message path: store first, then react, enqueue or ask (SPEC sections 4 and 6)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, Message, MessageReactionUpdated, ReactionTypeEmoji, ReplyParameters, User
from televibe.telegram import reaction_added

from telegrind.config import Config
from telegrind.incoming import AUTHOR_BOT, Incoming, incoming
from telegrind.layout import Layout
from telegrind.log import Entry, Log, now
from telegrind.receipts import FIRST, holds_heart, next_receipt
from telegrind.triggers import ADDRESS, holds_address, question_text

log = logging.getLogger(__name__)

VOICE_KINDS = ("voice", "video_note")
ADDRESSED_EMPTY = "Человек отметил это сообщение ❤, в нём нет текста. Посмотри, что это, и ответь."
NO_TRANSCRIPT_TEXT = "Это голосовое не получилось расшифровать."
SLOW_TRANSCRIPT_TEXT = "Голосовое всё ещё расшифровывается. Сними ❤ и поставь снова, когда появится расшифровка."
VOICE_WAIT_S = 600.0  # Scribe with its retries and the cleanup call; past this, the person is told


class Answerer(Protocol):
    async def answer(self, uid: int, message: Message, text: str, *, addressed: bool = False) -> None: ...


class JobQueue(Protocol):
    def enqueue(self, uid: int, inc: Incoming) -> None: ...


class MediaQueue(JobQueue, Protocol):
    async def wait(self, uid: int, message_id: int, timeout_s: float) -> bool: ...


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
        media: MediaQueue | None = None,
        voice: MediaQueue | None = None,
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
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("spawned task failed: %s: %s", type(exc).__name__, exc, exc_info=exc)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def close(self) -> None:
        for store in self.logs.values():
            store.close()
        self.logs.clear()

    def entry_changed(self, uid: int) -> None:
        """Tell the backlog watcher. It reads the agent's facts.sqlite, which the agent owns and may have left in
        any shape; a failure there costs this check, never the message (the next entry asks again)."""
        if self.watcher is None:
            return
        try:
            self.watcher.on_entry(uid)
        except Exception as exc:
            log.error("user %d: backlog check failed: %s", uid, type(exc).__name__)

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

    async def unsay(self, uid: int, message_id: int) -> None:
        """Delete a message the bot sent. Its log row stays: it was said. A failure is logged and ignored."""
        try:
            await self.bot.delete_message(chat_id=uid, message_id=message_id)
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            log.warning("delete of %d/%d failed: %s", uid, message_id, type(exc).__name__)

    # -- updates -----------------------------------------------------------------------

    async def on_message(self, msg: Message) -> None:
        uid = self.allowed(msg.chat, msg.from_user)
        if uid is None:
            return
        store = self.log_for(uid)
        inc = incoming(msg, now=self.clock())
        store.record_received(inc)  # REQ-LOG-1: committed before anything else
        # The job rows too, before any await: a crash in the reaction or the backlog check must not lose them,
        # since resume() finds a job by its row. The runners spawned here start at the next await.
        if inc.media is not None and self.media is not None:
            self.media.enqueue(uid, inc)
        if inc.voice is not None and self.voice is not None:
            self.voice.enqueue(uid, inc)
        question = question_text(inc.text, reply_to_author=store.author_of(inc.reply_to), author=inc.author)
        if question is None:
            await self.react(uid, inc.message_id, FIRST)
            self.entry_changed(uid)
        elif self.answerer is not None:
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
        self.entry_changed(uid)

    async def on_reaction(self, update: MessageReactionUpdated) -> None:
        uid = self.allowed(update.chat, update.user)
        if uid is None:
            return
        store = self.log_for(uid)
        author = store.author_of(update.message_id)
        if author is None:
            return  # a message the log never saw
        new = [r.emoji for r in update.new_reaction if isinstance(r, ReactionTypeEmoji)]
        old = [r.emoji for r in update.old_reaction if isinstance(r, ReactionTypeEmoji)]
        payload = update.model_dump(mode="json", exclude_none=True)
        if author == AUTHOR_BOT:  # REQ-REACT-1: ignored, but ❤ on a transcript quote addresses its voice note
            if (voice := await self._quoted_voice(uid, update.message_id)) is not None:
                await self._mark_addressed(uid, voice.message_id, old, new, payload)
                if reaction_added(update, ADDRESS):
                    await self.address(uid, voice)
            return
        entry = store.record_reaction(update.message_id, deleted=holds_heart(new), payload=payload, at=self.clock())
        if entry is not None:
            self.entry_changed(uid)
        await self._mark_addressed(uid, update.message_id, old, new, payload)
        if reaction_added(update, ADDRESS) and (target := store.entry(update.message_id)) is not None:
            await self.address(uid, target)

    async def _mark_addressed(
        self, uid: int, message_id: int, old: list[str], new: list[str], payload: dict[str, Any]
    ) -> None:
        """REQ-TRIGGER-4, REQ-RECEIPT-4: ❤ put takes the entry out of the log, and its 💔 with it;
        ❤ taken off brings both back. Only a change of ❤ itself counts, so 👍 beside it changes nothing."""
        if holds_address(old) == holds_address(new):
            return
        store = self.log_for(uid)
        entry = store.record_address(message_id, addressed=holds_address(new), payload=payload, at=self.clock())
        if entry is None:
            return
        self.entry_changed(uid)
        if entry.addressed_at is not None:
            if entry.receipt is not None:
                await self.react(uid, message_id, None)
        elif question_text(entry.text, reply_to_author=store.author_of(entry.reply_to), author=entry.author) is None:
            await self.react(uid, message_id, FIRST)  # a question was never a log entry: no heart for it

    async def _address_when_transcribed(self, uid: int, message_id: int) -> None:
        """REQ-VOICE-7: ❤ came before the transcript. Wait for it, and ask only if the ❤ is still there."""
        done = self.voice is None or await self.voice.wait(uid, message_id, VOICE_WAIT_S)
        entry = self.log_for(uid).entry(message_id)
        if entry is None or entry.addressed_at is None:
            return
        if entry.transcript:
            await self.address(uid, entry)
        else:
            await self.say(uid, NO_TRANSCRIPT_TEXT if done else SLOW_TRANSCRIPT_TEXT, message_id)

    async def _quoted_voice(self, uid: int, message_id: int) -> Entry | None:
        """The voice note a bot message quotes: a bot message replying to one, outside every answer chain."""
        store = self.log_for(uid)
        sent = store.entry(message_id)
        if sent is None or sent.reply_to is None or await store.chain_of(uid, message_id) is not None:
            return None
        voice = store.entry(sent.reply_to)
        return voice if voice is not None and voice.kind in VOICE_KINDS and voice.author != AUTHOR_BOT else None

    async def address(self, uid: int, entry: Entry) -> None:
        """REQ-TRIGGER-4, REQ-VOICE-7: start an addressed turn on a stored entry."""
        if entry.kind in VOICE_KINDS:
            if not entry.transcript:
                self.spawn(self._address_when_transcribed(uid, entry.message_id))
                return
            text = entry.transcript
        else:
            text = (entry.text or "").strip() or ADDRESSED_EMPTY
        if self.answerer is not None:
            message = Message.model_validate_json(entry.raw)  # televibe REQ-REACTED-2: a stored message drives a turn
            self.spawn(self.answerer.answer(uid, message, text, addressed=True))
