"""Agent turns: a pass over the tail, or an answer, through televibe (SPEC section 7)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import Message
from televibe import Access, Account, ClaudeCode, Done, Engine, Failed, Limits
from televibe.events import Event
from televibe.telegram import Chains, Presenter, Reactions, Texts

from telegrind.app import App
from telegrind.config import Config
from telegrind.facts import Facts

log = logging.getLogger(__name__)

TURN_ENV: Mapping[str, str] = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
NEVER_IN_A_TURN = frozenset({"BOT_TOKEN", "ELEVENLABS_API_KEY"})
# A prompt, not a password: ruff reads the "PASS" in the name as one.
PASS_PROMPT = "Разбери необработанные записи журнала по правилам CLAUDE.md. Закончи одной строкой: что извлечено."  # noqa: S105
NO_AGENT_TEXT = "Сейчас не могу ответить: агент не подключён. Запись сохранена."
QUOTA_TEXT = "На сегодня вопросы закончились (лимит {quota}). Запись сохранена; спроси завтра."
FAILED_TEXT = "Не получилось ответить. Запись сохранена, попробуй ещё раз."
TEXTS = Texts(
    timeout="Не успел ответить за отведённое время.",
    cancelled="Ответ отменён.",
    session_lost="Потерял нить разговора. Спроси новым сообщением, не ответом.",
    provider_error="Не получилось ответить.",
    spawn_error="Не смог запустить агента.",
    usage_limit="Лимит подписки исчерпан, сброс в {resets_at:%H:%M}.",
    five_hour="5ч",
    seven_day="7д",
)
REACTIONS = Reactions(done=None, failed_when_sent=False)  # a reaction or a text, never both
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def make_provider(config: Config) -> ClaudeCode | None:
    """REQ-TURN-1: the provider on the bot's own account; None without the token."""
    if not config.claude_token:
        return None
    return ClaudeCode(Account(config.account_home, credentials={"CLAUDE_CODE_OAUTH_TOKEN": config.claude_token}))


class _Watched:
    """A turn's events, passed through unchanged, remembering what the presenter will have sent."""

    def __init__(self, events: AsyncIterable[Event]) -> None:
        self._events = events
        self.end: Done | Failed | None = None
        self.limits: Limits | None = None

    async def __aiter__(self) -> AsyncIterator[Event]:
        async for event in self._events:
            if isinstance(event, Limits):
                self.limits = event
            elif isinstance(event, Done | Failed):
                self.end = event
            yield event

    def shown(self, tz: ZoneInfo) -> str:
        """The text the presenter built from the terminal event (presenter._finish), for the log."""
        if isinstance(self.end, Done):
            return self.end.text
        if isinstance(self.end, Failed):
            unix = self.limits.resets_at if self.limits is not None else None
            reason = TEXTS.for_reason(self.end.reason, datetime.fromtimestamp(unix, tz) if unix is not None else None)
            return f"{self.end.partial}\n\n{reason}" if self.end.partial.strip() else reason
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
        name = self.facts(uid).tz() or self.config.default_tz
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

    def instructions(self, uid: int, reply_text: str | None = None) -> str:
        """REQ-TURN-2: the current time in the person's zone, and the replied text when there is one."""
        local = self.local_now(uid)
        lines = [f"Сейчас {local:%Y-%m-%d %H:%M}, {WEEKDAYS[local.weekday()]}, зона {self.tz_of(uid).key}."]
        if reply_text:
            lines += ["Человек отвечает на это сообщение бота:", "<<<", reply_text, ">>>"]
        return "\n".join(lines)

    def quota_left(self, uid: int) -> int:
        used = self.app.log_for(uid).turns_since("question", self.day_start(uid))
        return max(0, self.config.turn_quota - used)

    def chains_for(self, uid: int) -> Chains:
        if uid not in self.chains:
            self.chains[uid] = Chains(self.app.log_for(uid))
        return self.chains[uid]

    # -- a question -----------------------------------------------------------------------

    async def answer(self, uid: int, message: Message, text: str) -> None:
        store = self.app.log_for(uid)
        if self.engine is None or self.provider is None:
            await self.app.say(uid, NO_AGENT_TEXT, message.message_id)
            return
        if self.quota_left(uid) <= 0:
            await self.app.say(uid, QUOTA_TEXT.format(quota=self.config.turn_quota), message.message_id)
            return
        reply_text = None
        if message.reply_to_message is not None:
            replied = store.entry(message.reply_to_message.message_id)
            if replied is not None:
                reply_text = replied.transcript or replied.text
            else:
                reply_text = message.reply_to_message.text or message.reply_to_message.caption
        tz = self.tz_of(uid)
        chains = self.chains_for(uid)
        presenter = Presenter(self.app.bot, message, reactions=REACTIONS, texts=TEXTS, tz=tz)
        await presenter.accepted()
        store.turn_add("question", self.app.clock())
        try:
            chain = await chains.find(message) or chains.new()
            async with chains.turn(
                self.engine,
                chain,
                message,
                provider=self.provider,
                cwd=self.app.layout.user_dir(uid),
                prompt=text,
                access=Access.FULL,
                lane=str(uid),
                instructions=self.instructions(uid, reply_text),
                env=self.env(),
                timeout_s=self.config.question_timeout_s,
                tag={"kind": "question", "user": uid},
            ) as turn:
                watched = _Watched(turn)
                sent = await presenter.show(watched)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # entering the chain turn failed: 👀 is still on the question; say so
            log.warning("user %d: question turn failed before it ran: %s", uid, type(exc).__name__)
            await self.app.react(uid, message.message_id, None)
            await self.app.say(uid, FAILED_TEXT, message.message_id)
            return
        if sent is not None:
            await chains.link(chain, sent)
            # A rich message comes back without `text`: store what the presenter sent (REQ-LOG-7).
            store.record_bot_sent(
                sent.message_id,
                sent.text or watched.shown(tz),
                message.message_id,
                sent.model_dump(mode="json", exclude_none=True),
                self.app.clock(),
            )

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

    async def run_pass(self, uid: int) -> None:
        if self.engine is None or self.provider is None:
            return
        store, facts = self.app.log_for(uid), self.facts(uid)
        before = facts.mark()
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
                if isinstance(event, Done | Failed):
                    end = event
        if not isinstance(end, Done):
            reason = end.reason if isinstance(end, Failed) else "no terminal event"
            log.warning("user %d: pass failed: %s", uid, reason)
            return
        digest = end.text.strip()
        if facts.mark() != before and digest:  # REQ-TURN-7
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
