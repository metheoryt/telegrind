"""The message path: store first, then react, enqueue or ask (SPEC sections 4 and 6)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    MessageReactionUpdated,
    PollAnswer,
    ReactionTypeEmoji,
    ReplyParameters,
    User,
)
from televibe.telegram import reaction_added
from televibe.telegram.controls import (
    Control,
    ControlTexts,
    Form,
    Offer,
    Poll,
    answered_keyboard,
    form_answer,
    keyboard,
    offer_answer,
    parse_press,
    poll_answer,
    split_control,
    ticks_after,
)

from telegrind.config import Config
from telegrind.incoming import AUTHOR_BOT, AUTHOR_ME, Incoming, incoming
from telegrind.layout import Layout
from telegrind.log import Entry, Log, now
from telegrind.receipts import FIRST, holds_heart, next_receipt
from telegrind.triggers import ADDRESS, holds_address, question_text

log = logging.getLogger(__name__)

VOICE_KINDS = ("voice", "video_note")
ADDRESSED_EMPTY = "Человек отметил это сообщение ❤, в нём нет текста. Посмотри, что это, и ответь."
NO_TRANSCRIPT_TEXT = "Это голосовое не получилось расшифровать"
SLOW_TRANSCRIPT_TEXT = "Голосовое всё ещё расшифровывается. Сними ❤ и поставь снова, когда появится расшифровка"
VOICE_WAIT_S = 600.0  # Scribe with its retries and the cleanup call; past this, the person is told
CONTROL_TEXTS = ControlTexts(  # REQ-TURN-13; here, not in agent.py, which imports this module
    offer_label="Да",
    done_label="Готово",
    pressed="Ответ кнопкой: {label}",
    voted="Выбрано в опросе: {options}",
    nothing="ничего",
    form="Анкета: {rows}",
    unset="не выбрано",
    typed="Текстом: {text}",
    answered="✓ {label}",
    answered_in_text="✓ ответ текстом",
)


class Answerer(Protocol):
    async def answer(
        self, uid: int, message: Message, text: str, *, addressed: bool = False, answering: int | None = None
    ) -> None: ...


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

    async def telegram(self, uid: int, what: str, call: Coroutine[Any, Any, Any]) -> None:
        """A Telegram call whose failure costs only itself: logged, and the caller goes on (REQ-TRIGGER-6)."""
        try:
            await call
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            log.warning("user %d: %s failed: %s", uid, what, type(exc).__name__)

    async def say(self, uid: int, text: str, reply_to: int | None) -> Message | None:
        """Send and store (REQ-LOG-7). Text the model may have written goes out with parse_mode=None, and without
        a control block: only a question's answer carries a control (REQ-TURN-14)."""
        text = split_control(text)[0]
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
        # The job rows too, before any await: a crash in the reaction or the backlog check must not lose them,
        # since resume() finds a job by its row. The runners spawned here start at the next await.
        if inc.media is not None and self.media is not None:
            self.media.enqueue(uid, inc)
        if inc.voice is not None and self.voice is not None:
            self.voice.enqueue(uid, inc)
        question = question_text(
            inc.text, reply_to_author=store.author_of(inc.reply_to), author=inc.author, wordless=inc.voice is None
        )
        if question is None:
            await self.react(uid, inc.message_id, FIRST)
            self.entry_changed(uid)
            return
        question = await self._settle_reply(uid, inc.reply_to, question)
        if self.answerer is not None:
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
        elif self._question_of(uid, entry) is None:
            await self.react(uid, message_id, FIRST)  # a question was never a log entry: no heart for it

    def _question_of(self, uid: int, entry: Entry) -> str | None:
        """REQ-TRIGGER-1, REQ-VOICE-7: an entry is a question by its text, and a voice note's text is its transcript."""
        voice = entry.kind in VOICE_KINDS
        text = entry.transcript if voice else entry.text
        return question_text(
            text, reply_to_author=self.log_for(uid).author_of(entry.reply_to), author=entry.author, wordless=not voice
        )

    async def on_transcript(self, uid: int, message_id: int) -> None:
        """REQ-VOICE-7: a transcribed note that would be a question as text is asked now; its heart comes off,
        as a question was never a log entry. A note under ❤ is already asked by the ❤ path. REQ-VOICE-8: a job
        that ended with no transcript tells a note replying to the bot so, as the ❤ path does."""
        store = self.log_for(uid)
        entry = store.entry(message_id)
        if entry is None or entry.addressed_at is not None:
            return
        if not entry.transcript:
            if entry.author == AUTHOR_ME and store.author_of(entry.reply_to) == AUTHOR_BOT:
                await self.say(uid, NO_TRANSCRIPT_TEXT, message_id)
            return
        if (question := self._question_of(uid, entry)) is None:
            return
        question = await self._settle_reply(uid, entry.reply_to, question)  # REQ-TRIGGER-7: before any other call
        if entry.receipt is not None:
            await self.react(uid, message_id, None)
        self.entry_changed(uid)
        if self.answerer is not None:
            message = Message.model_validate_json(entry.raw)
            self.spawn(self.answerer.answer(uid, message, question))

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

    # -- controls: a press, a vote, or a reply to a control is the person's answer ------------

    async def on_callback(self, query: CallbackQuery) -> None:
        """REQ-TRIGGER-6, REQ-LOG-1: the press is logged, then the callback answered, then the keyboard edited and,
        when it closed the control, the answer asked. Only the message's chat and id are read: it may be an
        InaccessibleMessage. Anything not ours (a stranger, a message the log never saw, no control, foreign data)
        only stops the spinner."""
        msg, press = query.message, parse_press(query.data)
        uid = self.allowed(msg.chat, query.from_user) if msg is not None else None
        if msg is None or uid is None or press is None or query.data is None:
            await self.telegram(
                query.from_user.id, "callback answer", self.bot.answer_callback_query(callback_query_id=query.id)
            )
            return
        store, mid = self.log_for(uid), msg.message_id
        payload = query.model_dump(mode="json", exclude_none=True)
        closed = store.record_press(mid, query.data, payload, self.clock(), closes=press.kind in ("offer", "done"))
        await self.telegram(uid, "callback answer", self.bot.answer_callback_query(callback_query_id=query.id))
        rec = store.control(mid)
        if rec is None:
            return  # no control on that message: record_press stored nothing
        control = split_control(rec.block, quiet=True)[1]
        if closed:
            await self._answered(uid, mid, control, rec.presses)
        elif rec.open and press.kind == "tick" and isinstance(control, Form):
            markup = keyboard(control, CONTROL_TEXTS, self._ticks(control, rec.presses))
            await self.telegram(
                uid, "tick", self.bot.edit_message_reply_markup(chat_id=uid, message_id=mid, reply_markup=markup)
            )

    async def _answered(self, uid: int, mid: int, control: Control | None, presses: tuple[str, ...]) -> None:
        """An offer or a form [Готово] press closed the control: mark the keyboard, then ask the answer."""
        if isinstance(control, Offer):
            label, text = control.label or CONTROL_TEXTS.offer_label, offer_answer(control, CONTROL_TEXTS)
        elif isinstance(control, Form):
            label = CONTROL_TEXTS.done_label
            text = form_answer(control, self._ticks(control, presses), CONTROL_TEXTS)
        else:
            log.warning("user %d: a press closed %d, whose block is not an offer or a form", uid, mid)
            return
        mark = answered_keyboard(CONTROL_TEXTS.answered.format(label=label))
        await self.telegram(
            uid, "answered mark", self.bot.edit_message_reply_markup(chat_id=uid, message_id=mid, reply_markup=mark)
        )
        self._ask_on(uid, mid, text)

    async def on_poll_answer(self, answer: PollAnswer) -> None:
        """REQ-TRIGGER-6: a vote by an allowed person is logged; one that closed the poll stops it and is asked.
        A retracted vote (no options) is logged and starts nothing. A private chat's id is the person's."""
        uid = answer.user.id if answer.user is not None else None
        if uid is None or uid not in self.config.allowed_user_ids:
            return
        store = self.log_for(uid)
        payload = answer.model_dump(mode="json", exclude_none=True)
        voted = store.record_vote(answer.poll_id, payload, self.clock(), closes=bool(answer.option_ids))
        if voted is None or not voted[1]:
            return
        mid = voted[0]
        await self.telegram(uid, "stop_poll", self.bot.stop_poll(chat_id=uid, message_id=mid))
        rec = store.control(mid)
        poll = split_control(rec.block, quiet=True)[1] if rec is not None else None
        if not isinstance(poll, Poll):
            log.warning("user %d: a vote closed %d, whose block is not a poll", uid, mid)
            return
        chosen = [i for i in answer.option_ids if 0 <= i < len(poll.options)]
        self._ask_on(uid, mid, poll_answer(poll, chosen, CONTROL_TEXTS))

    def _ask_on(self, uid: int, mid: int, text: str) -> None:
        """A question turn on the answered bot message itself (REQ-TRIGGER-6)."""
        entry = self.log_for(uid).entry(mid)
        if self.answerer is None or entry is None:
            return
        message = Message.model_validate_json(entry.raw)
        self.spawn(self.answerer.answer(uid, message, text, answering=mid))

    async def _settle_reply(self, uid: int, reply_to: int | None, text: str) -> str:
        """REQ-TRIGGER-7: a text or voice reply to a message with an open control closes it: in the log first, then
        the poll stopped or the keyboard marked. A form's ticks go with the text. Returns the question's text."""
        store = self.log_for(uid)
        rec = store.control(reply_to) if reply_to is not None else None
        if reply_to is None or rec is None or not store.close_control(reply_to, "text", self.clock()):
            return text
        if rec.poll_id is not None:
            await self.telegram(uid, "stop_poll", self.bot.stop_poll(chat_id=uid, message_id=reply_to))
        else:
            mark = answered_keyboard(CONTROL_TEXTS.answered_in_text)
            await self.telegram(
                uid,
                "answered mark",
                self.bot.edit_message_reply_markup(chat_id=uid, message_id=reply_to, reply_markup=mark),
            )
        control = split_control(rec.block, quiet=True)[1]
        if isinstance(control, Form):
            return form_answer(control, self._ticks(control, rec.presses), CONTROL_TEXTS, typed=text)
        return text

    @staticmethod
    def _ticks(form: Form, presses: tuple[str, ...]) -> tuple[int | None, ...]:
        """REQ-TRIGGER-6: the ticks replayed from the log's presses, so they survive a restart."""
        return ticks_after(form, [p for p in map(parse_press, presses) if p is not None])
