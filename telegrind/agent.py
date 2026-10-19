"""Agent turns: a pass over the tail, or an answer, through televibe (SPEC section 7)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from televibe import Access, Account, ClaudeCode, Done, Engine, Failed, Limits, Started, TelevibeError
from televibe.events import Event
from televibe.events import Message as AgentMessage
from televibe.telegram import Chain, Chains, Presenter, Reactions, Texts
from televibe.telegram.controls import (
    Control,
    Form,
    Offer,
    Poll,
    control_block,
    poll_fallback_text,
    split_control,
)

from telegrind.app import CONTROL_TEXTS, App
from telegrind.config import Config
from telegrind.editor import EditorError, edit, without_final_period
from telegrind.facts import Facts, FactsUnavailable
from telegrind.incoming import AUTHOR_BOT, AUTHOR_ME, author_of, media_of
from telegrind.log import Entry

log = logging.getLogger(__name__)

TURN_ENV: Mapping[str, str] = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
NEVER_IN_A_TURN = frozenset({"BOT_TOKEN", "ELEVENLABS_API_KEY"})
# A prompt, not a password: ruff reads the "PASS" in the name as one.
PASS_PROMPT = "Разбери необработанные записи журнала по правилам CLAUDE.md. Закончи одной строкой: что извлечено."  # noqa: S105
NO_AGENT_TEXT = "Сейчас не могу ответить: агент не подключён. Запись сохранена"
QUOTA_TEXT = "На сегодня вопросы закончились (лимит {quota}). Запись сохранена; спроси завтра"
FAILED_TEXT = "Не получилось ответить. Запись сохранена, попробуй ещё раз"
TEXTS = Texts(
    timeout="Не успела ответить за отведённое время",
    cancelled="Ответ отменён",
    session_lost="Потеряла нить разговора. Спроси новым сообщением, не ответом",
    provider_error="Не получилось ответить",
    spawn_error="Не смогла начать ответ",
    usage_limit="Лимит подписки исчерпан, сброс в {resets_at:%H:%M}",
    five_hour="5ч",
    seven_day="7д",
)
REACTIONS = Reactions(
    working="👀", done=None, failed_when_sent=False
)  # a reaction or a text, never both; 👀 throughout
LONG_TURN_S = 60.0  # REQ-TURN-11: a question still running after this is told it is long
LONG_TURN_TEXT = "Это надолго. Отвечу сюда, как закончу; записи можно слать дальше"
MEDIA_WAIT_S = 30.0  # how long a question with a photo or document waits for its own download
REPLY_INTRO = {AUTHOR_BOT: "Человек отвечает на это сообщение бота:", AUTHOR_ME: "Человек отвечает на свою запись:"}
ABOVE_INTRO = "Ответ бота, под которым стоит сообщение ниже:"  # a poll's answer (REQ-TRIGGER-6, REQ-TRIGGER-7)
FORWARD_INTRO = "Человек отвечает на пересланное сообщение:"  # any other author is a forward
ADDRESSED_INTRO = "Человек отметил это сообщение ❤: оно обращено к тебе. Оно написано {when}."
CONTEXT_INTRO = "Записи журнала перед ним (время, автор, текст):"
CONTEXT_SIZE = 10  # REQ-TRIGGER-5: about ten entries, as a pass reads (REQ-CONTRACT-2)
DELETED_MARK = " (удалено)"
ASIDE_EVERY = 5  # REQ-CONTRACT-17: one question turn in this many may end with a question by the way
ASIDE_TEXT = "В этом ответе можно спросить между прочим (см. «Знакомство»)."
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def make_provider(config: Config) -> ClaudeCode | None:
    """REQ-TURN-1: the provider on the bot's own account; None without the token."""
    if not config.claude_token:
        return None
    return ClaudeCode(Account(config.account_home, credentials={"CLAUDE_CODE_OAUTH_TOKEN": config.claude_token}))


class _Watched:
    """A turn's events, remembering what the presenter will have sent.

    With an editor (REQ-TURN-12) the agent's messages never reach the draft, and `Done` carries the edited text;
    `draft` keeps the agent's own. The editor sees the answer without its control block, which is put back after
    it (REQ-TURN-13); a block in the editor's own text is dropped first, so the answer carries the agent's one.
    Without one, the events pass through unchanged."""

    def __init__(self, events: AsyncIterable[Event], editor: Callable[[str], Awaitable[str]] | None = None) -> None:
        self._events, self._editor = events, editor
        self.end: Done | Failed | None = None
        self.limits: Limits | None = None
        self.draft: str | None = None
        self.control: Control | None = None
        """The control `Done` carries, whether or not the presenter could send it as one."""
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[Event]:
        async for event in self._events:
            if isinstance(event, Started):
                self.started.set()
            elif isinstance(event, Limits):
                self.limits = event
            elif isinstance(event, AgentMessage) and self._editor is not None:
                continue
            elif isinstance(event, Done) and self._editor is not None:
                visible, control = split_control(event.text)
                if visible.strip():
                    self.draft = event.text
                    # A fence the editor wrote itself would make two blocks, and both would be dropped.
                    edited = split_control(await self._edited(visible), quiet=True)[0].strip() or visible
                    block = control_block(control) if control is not None else ""
                    event = dataclasses.replace(event, text="\n\n".join(p for p in (edited, block) if p))
            if isinstance(event, Done):
                self.control = split_control(event.text, quiet=True)[1]
            if isinstance(event, Done | Failed):
                self.end = event
            yield event

    async def _edited(self, text: str) -> str:
        """The editor's text, or the agent's own when the editor fails: an answer is never lost to it."""
        assert self._editor is not None
        try:
            return await self._editor(text)
        except (TelevibeError, EditorError) as exc:
            log.warning("answer editor failed (%s); sending the agent's own text", type(exc).__name__)
            return text

    def shown(self, tz: ZoneInfo) -> str:
        """The text the presenter built from the terminal event (presenter._finish), for the log: the visible
        text, never the control block (REQ-TURN-13)."""
        if isinstance(self.end, Done):
            text, control = split_control(self.end.text, quiet=True)
            if not text.strip() and control is not None:  # a block alone goes out as this (REQ-PRESENT-6)
                return control.question if isinstance(control, Poll) else "…"
            return text
        if isinstance(self.end, Failed):
            unix = self.limits.resets_at if self.limits is not None else None
            reason = TEXTS.for_reason(self.end.reason, datetime.fromtimestamp(unix, tz) if unix is not None else None)
            partial = split_control(self.end.partial, quiet=True)[0]
            return f"{partial}\n\n{reason}" if partial.strip() else reason
        return ""


class Agent:
    def __init__(
        self,
        *,
        config: Config,
        app: App,
        engine: Engine | None,
        provider: ClaudeCode | None,
        turn_env: Mapping[str, str] = TURN_ENV,
    ) -> None:
        self.config, self.app, self.engine, self.provider = config, app, engine, provider
        self.turn_env = dict(turn_env)
        self.chains: dict[int, Chains] = {}
        self.passes: dict[int, asyncio.Task[None]] = {}

    def env(self) -> dict[str, str]:
        """TURN_ENV always, then the caller's extras; never the bot's own secrets."""
        merged = {**TURN_ENV, **self.turn_env}
        return {k: v for k, v in merged.items() if k not in NEVER_IN_A_TURN}

    # -- the person's clock -------------------------------------------------------------

    def facts(self, uid: int) -> Facts:
        return Facts(self.app.layout.facts_path(uid))

    def tz_of(self, uid: int) -> ZoneInfo:
        try:
            name = self.facts(uid).tz() or self.config.default_tz
        except FactsUnavailable as exc:
            log.warning("user %d: facts.sqlite unavailable (%s); using %s", uid, exc.reason, self.config.default_tz)
            name = self.config.default_tz
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("user %d: settings.tz %r is not a zone; using %s", uid, name, self.config.default_tz)
            return ZoneInfo(self.config.default_tz)

    def local_now(self, uid: int) -> datetime:
        return datetime.fromtimestamp(self.app.clock(), self.tz_of(uid))

    def day_start(self, uid: int) -> int:
        local = self.local_now(uid)
        return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def instructions(
        self,
        uid: int,
        reply_text: str | None = None,
        reply_author: str = AUTHOR_BOT,
        addressed: Message | None = None,
        *,
        aside: bool = False,
        above: str | None = None,
    ) -> str:
        """REQ-TURN-2: the current time in the person's zone, and the replied text, introduced by who wrote it.
        REQ-TRIGGER-5: for a message addressed with ❤, when it was written and the entries before it.
        REQ-TRIGGER-6/7: `above`, the answer a replied poll stands under, is quoted before it."""
        local = self.local_now(uid)
        lines = [f"Сейчас {local:%Y-%m-%d %H:%M}, {WEEKDAYS[local.weekday()]}, зона {self.tz_of(uid).key}."]
        if addressed is not None:
            lines += self._addressed(uid, addressed)
        if above and reply_text:
            lines += [ABOVE_INTRO, "<<<", above, ">>>"]
        if reply_text:
            lines += [REPLY_INTRO.get(reply_author, FORWARD_INTRO), "<<<", reply_text, ">>>"]
        if aside:
            lines.append(ASIDE_TEXT)
        return "\n".join(lines)

    def _addressed(self, uid: int, message: Message) -> list[str]:
        store, tz = self.app.log_for(uid), self.tz_of(uid)
        entry = store.entry(message.message_id)
        written = entry.occurred_at if entry is not None else int(message.date.timestamp())
        replied = message.reply_to_message
        pivot = replied.message_id if replied is not None and store.entry(replied.message_id) else message.message_id
        lines = [ADDRESSED_INTRO.format(when=f"{datetime.fromtimestamp(written, tz):%Y-%m-%d %H:%M}")]
        context = store.context_before(pivot, CONTEXT_SIZE)
        if context:
            lines += [CONTEXT_INTRO, "<<<"]
            for e in context:
                said = e.transcript or e.text or f"[{e.kind}]"
                mark = DELETED_MARK if e.deleted_at is not None else ""
                lines.append(f"{datetime.fromtimestamp(e.occurred_at, tz):%Y-%m-%d %H:%M} {e.author}: {said}{mark}")
            lines.append(">>>")
        return lines

    def _answer_above(self, uid: int, replied: Entry) -> str | None:
        """The bot answer a replied poll message stands under (REQ-TRIGGER-6, REQ-TRIGGER-7). Only a poll: a press
        turn's answer replies to a bot answer too; a poll's numbered text needs none (its chain wrote it)."""
        store = self.app.log_for(uid)
        rec = store.control(replied.message_id)
        if replied.author != AUTHOR_BOT or replied.reply_to is None or rec is None or rec.poll_id is None:
            return None
        parent = store.entry(replied.reply_to)
        return (parent.transcript or parent.text) if parent is not None and parent.author == AUTHOR_BOT else None

    def quota_left(self, uid: int) -> int:
        used = self.app.log_for(uid).turns_since("question", self.day_start(uid))
        return max(0, self.config.turn_quota - used)

    def chains_for(self, uid: int) -> Chains:
        if uid not in self.chains:
            self.chains[uid] = Chains(self.app.log_for(uid))
        return self.chains[uid]

    # -- a question -----------------------------------------------------------------------

    async def answer(
        self, uid: int, message: Message, text: str, *, addressed: bool = False, answering: int | None = None
    ) -> None:
        """A question turn. An addressed one (REQ-TRIGGER-4) carries the context around its entry. One `answering` a
        control (REQ-TRIGGER-6) runs on that bot message itself: it quotes it and continues its chain."""
        store = self.app.log_for(uid)
        if self.engine is None or self.provider is None:
            await self.app.say(uid, NO_AGENT_TEXT, message.message_id)
            return
        if self.quota_left(uid) <= 0:
            await self.app.say(uid, QUOTA_TEXT.format(quota=self.config.turn_quota), message.message_id)
            return
        reply_text, reply_author, above = None, AUTHOR_BOT, None
        reply = message.reply_to_message
        replied_id = answering if answering is not None else (reply.message_id if reply is not None else None)
        replied = store.entry(replied_id) if replied_id is not None else None
        if replied is not None:
            reply_text, reply_author = replied.transcript or replied.text, replied.author
            above = self._answer_above(uid, replied)
        elif answering is None and reply is not None:
            reply_text = reply.text or reply.caption
            reply_author = author_of(reply)
        tz = self.tz_of(uid)
        chains = self.chains_for(uid)
        presenter = Presenter(self.app.bot, message, reactions=REACTIONS, texts=TEXTS, tz=tz, controls=CONTROL_TEXTS)
        await presenter.accepted()
        # A question about its own photo or document waits for that file, bounded; the download is not cancelled.
        media = self.app.media if media_of(message) is not None else None
        if media is not None and not await media.wait(uid, message.message_id, MEDIA_WAIT_S):
            log.warning(
                "user %d: media of %d not stored in %s s; answering without it", uid, message.message_id, MEDIA_WAIT_S
            )
        store.turn_add("question", self.app.clock())
        aside = store.turns_since("question", 0) % ASIDE_EVERY == 0
        try:
            found = await chains.of(message.chat.id, answering) if answering is not None else await chains.find(message)
            chain = found or chains.new()
            async with chains.turn(
                self.engine,
                chain,
                message,
                provider=self.provider,
                cwd=self.app.layout.user_dir(uid),
                prompt=text,
                access=Access.FULL,
                lane=str(uid),
                instructions=self.instructions(
                    uid, reply_text, reply_author, message if addressed else None, aside=aside, above=above
                ),
                env=self.env(),
                timeout_s=self.config.question_timeout_s,
                tag={"kind": "question", "user": uid},
            ) as turn:
                watched = _Watched(turn, self._editor(text, reply_text) if self.config.edit_answers else None)
                notice = asyncio.create_task(self._note_if_long(presenter, watched))
                try:
                    sent = await presenter.show(watched)
                finally:
                    notice.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # entering the chain turn failed: 👀 is still on the question; say so
            log.warning("user %d: question turn failed before it ran: %s", uid, type(exc).__name__)
            await self.app.react(uid, message.message_id, None)
            await self.app.say(uid, FAILED_TEXT, message.message_id)
            return
        if sent is None:
            return
        at = self.app.clock()
        await chains.link(chain, sent)
        # A rich message comes back without `text`: store what the presenter sent (REQ-LOG-7).
        store.record_bot_sent(
            sent.message_id,
            sent.text or watched.shown(tz),
            message.message_id,
            sent.model_dump(mode="json", exclude_none=True),
            at,
            draft=watched.draft if watched.draft != watched.shown(tz) else None,
        )
        control = presenter.control
        if isinstance(control, Offer | Form):  # REQ-TURN-13; a keyboard Telegram refused leaves it None
            store.record_control(sent.message_id, control_block(control), None, at)
        follow_up = presenter.follow_up
        if follow_up is not None and isinstance(watched.control, Poll):  # the poll, or its numbered text
            store.record_bot_sent(
                follow_up.message_id,
                poll_fallback_text(watched.control),
                sent.message_id,
                follow_up.model_dump(mode="json", exclude_none=True),
                at,
            )
            await chains.link(chain, follow_up)
            if isinstance(control, Poll) and follow_up.poll is not None:
                store.record_control(follow_up.message_id, control_block(control), follow_up.poll.id, at)
        keep = presenter.control_message.message_id if presenter.control_message is not None else None
        await self._supersede(uid, message.chat.id, chain, keep, at)

    async def _supersede(self, uid: int, chat_id: int, chain: Chain, keep: int | None, at: int) -> None:
        """REQ-TURN-13: a newer answer in the chain closes every other open control, then takes it off the chat.

        Closed in the log first, so a control a press answered meanwhile is left as the press left it."""
        store = self.app.log_for(uid)
        for rec in store.open_controls(chain.id):
            if rec.message_id == keep or not store.close_control(rec.message_id, "superseded", at):
                continue
            try:
                if rec.poll_id is not None:
                    await self.app.bot.stop_poll(chat_id=chat_id, message_id=rec.message_id)
                else:
                    await self.app.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=rec.message_id, reply_markup=None
                    )
            except (TelegramAPIError, OSError, TimeoutError) as exc:
                log.warning("user %d: removing the control of %d failed: %s", uid, rec.message_id, type(exc).__name__)

    def _editor(self, question: str, replied: str | None) -> Callable[[str], Awaitable[str]]:
        """REQ-TURN-12: the full answer rewritten for the person, with the question it answers."""
        provider = self.provider
        assert provider is not None

        async def run(draft: str) -> str:
            return await edit(draft, question=question, replied=replied, provider=provider, env=self.env())

        return run

    @staticmethod
    async def _note_if_long(presenter: Presenter, watched: _Watched) -> None:
        """REQ-TURN-11: LONG_TURN_S after the turn started, the notice goes into the draft the answer replaces."""
        await watched.started.wait()
        await asyncio.sleep(LONG_TURN_S)
        presenter.note(LONG_TURN_TEXT)

    # -- a pass ---------------------------------------------------------------------------

    def start_pass(self, uid: int) -> bool:
        """REQ-SCHED-2: at most one pending pass per person."""
        pending = self.passes.get(uid)
        if pending is not None and not pending.done():
            return False
        task = asyncio.get_running_loop().create_task(self.run_pass(uid), name=f"pass-{uid}")
        self.passes[uid] = task

        def done(t: asyncio.Task[None]) -> None:
            if self.passes.get(uid) is t:
                del self.passes[uid]
            if not t.cancelled() and (exc := t.exception()) is not None:
                log.error("user %d: pass crashed: %s", uid, type(exc).__name__, exc_info=exc)

        task.add_done_callback(done)
        return True

    @staticmethod
    def _mark(uid: int, facts: Facts, when: str) -> tuple[int, int] | None:
        """facts.mark(), or None when facts.sqlite cannot be read now: the change is then unknown."""
        try:
            return facts.mark()
        except FactsUnavailable as exc:
            log.warning("user %d: facts.sqlite unavailable (%s) %s; change unknown", uid, exc.reason, when)
            return None

    async def run_pass(self, uid: int) -> None:
        if self.engine is None or self.provider is None:
            return
        store, facts = self.app.log_for(uid), self.facts(uid)
        # Entering only queues the turn behind the lane; a question ahead of it runs a pass of its own and
        # writes processed. So `before` is measured at Started, and at entry only if no Started ever came.
        at_entry, before, started = self._mark(uid, facts, "at entry"), None, False
        store.turn_add("pass", self.app.clock())
        session = self.provider.new_session(self.app.layout.user_dir(uid))
        end: Done | Failed | None = None
        async with self.engine.turn(
            PASS_PROMPT,
            session=session,
            access=Access.FULL,
            lane=str(uid),
            instructions=self.instructions(uid),
            env=self.env(),
            timeout_s=self.config.pass_timeout_s,
            tag={"kind": "pass", "user": uid},
        ) as turn:
            async for event in turn:
                if isinstance(event, Started) and not started:
                    started, before = True, self._mark(uid, facts, "at start")
                elif isinstance(event, Done | Failed):
                    end = event
        if not isinstance(end, Done):
            reason = end.reason if isinstance(end, Failed) else "no terminal event"
            log.warning("user %d: pass failed: %s", uid, reason)
            return
        digest = without_final_period(split_control(end.text)[0].strip())  # REQ-TURN-14: no control on a digest
        baseline, after = (before if started else at_entry), self._mark(uid, facts, "after")
        if baseline is None or after is None:  # REQ-TURN-7 cannot be decided: post nothing rather than guess
            log.warning("user %d: pass done, change unknown; digest not posted", uid)
            return
        if after != baseline and digest:  # REQ-TURN-7
            await self.app.say(uid, digest, None)

    # -- start-up -------------------------------------------------------------------------

    async def recover(self) -> int:
        """REQ-TURN-9: log and forget what an earlier process left in flight."""
        if self.engine is None:
            return 0
        stranded = await self.engine.stranded()
        for s in stranded:
            log.warning(
                "stranded turn %s (%s) from %s; killed=%s; forgotten",
                s.turn_id,
                s.tag,
                s.started_at.isoformat(),
                s.killed,
            )
            self.engine.forget(s.turn_id)
        return len(stranded)
