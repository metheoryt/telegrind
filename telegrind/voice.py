"""Voice and video notes: Scribe, cleanup through the one-shot call, the transcript post (SPEC REQ-VOICE-1..8)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx
from aiogram.exceptions import TelegramAPIError
from aiogram.types import MessageEntity, ReplyParameters
from televibe import TelevibeError

from telegrind.agent import NEVER_IN_A_TURN, TURN_ENV, Agent
from telegrind.app import App
from telegrind.cleanup import Cleaned, CleanupError, clean
from telegrind.facts import Facts, FactsUnavailable
from telegrind.incoming import Incoming
from telegrind.keyterms import filter_terms
from telegrind.media import BACKOFF_S, MAX_ATTEMPTS, Running, download
from telegrind.scribe import ScribeError, transcribe

log = logging.getLogger(__name__)

PREFIX = "🎙 "  # inside the quote, so it reads as part of it
TG_LIMIT = 4096
FOLD_AT = 500  # a longer transcript is a folded quote: the chat sees a few lines, a tap shows the rest
MAX_SECONDS = 20 * 60  # a stray hour-long recording is not worth transcribing


def _units(text: str) -> int:
    """Telegram counts a message's length in UTF-16 code units: an emoji is two."""
    return len(text.encode("utf-16-le")) // 2


def _cut(para: str, limit: int) -> tuple[str, str]:
    """The longest head of `para` within `limit`, ending between words unless that wastes half of it."""
    n = min(len(para), limit)
    while _units(para[:n]) > limit:
        n -= 1
    space = para.rfind(" ", 0, n + 1)
    if space > n // 2:  # a space in the first half would waste most of the part
        return para[:space], para[space + 1 :]
    return para[:n], para[n:]


def chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Text as messages of at most `limit`, each opening with PREFIX, split between paragraphs, else between words."""
    room = limit - _units(PREFIX)  # every part carries the prefix (REQ-VOICE-6)
    pieces = []
    for para in text.strip().split("\n\n"):
        while _units(para) > room:
            head, para = _cut(para, room)
            pieces.append(head)
        pieces.append(para)
    out: list[str] = []
    for piece in pieces:
        if out and _units(out[-1]) + 2 + _units(piece) <= room:
            out[-1] += "\n\n" + piece
        else:
            out.append(piece)
    return [PREFIX + part for part in out]


def quote(part: str) -> list[MessageEntity]:
    """A transcript part is posted as one quote. Lengths count UTF-16 units, as Telegram does."""
    length = _units(part)
    kind = "expandable_blockquote" if length > FOLD_AT else "blockquote"
    return [MessageEntity(type=kind, offset=0, length=length)]


class VoiceJobs:
    """REQ-VOICE-1: a background job per voice note; nothing else waits for it."""

    def __init__(
        self,
        app: App,
        *,
        agent: Agent | None,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ask_env: Mapping[str, str] = TURN_ENV,
    ) -> None:
        self.app, self.agent, self.client, self.sleep, self.ask_env = app, agent, client, sleep, dict(ask_env)
        self._slots = asyncio.Semaphore(2)
        self.running = Running(app)

    async def wait(self, uid: int, message_id: int, timeout_s: float) -> bool:
        """REQ-VOICE-7: ❤ put before the transcript waits for it here."""
        return await self.running.wait(uid, message_id, timeout_s)

    def env(self) -> dict[str, str]:
        """The one-shot call's env, as Agent.env() builds a turn's: TURN_ENV, the extras, never the bot's secrets.

        Built at call time, so an `ask_env` assigned after construction is stripped too."""
        merged = {**TURN_ENV, **self.ask_env}
        return {k: v for k, v in merged.items() if k not in NEVER_IN_A_TURN}

    def enqueue(self, uid: int, inc: Incoming) -> None:
        assert inc.voice is not None
        store = self.app.log_for(uid)
        store.voice_enqueue(inc.message_id, inc.voice.file_id)
        if not self.app.config.elevenlabs_key:
            store.voice_set(inc.message_id, "skipped", error="no ELEVENLABS_API_KEY")
            return
        if inc.voice.duration > MAX_SECONDS:
            store.voice_set(inc.message_id, "too_long", error=f"{inc.voice.duration} s")
            return
        self.running.start(uid, inc.message_id, self.run(uid, inc.message_id, inc.voice.filename))

    def resume(self) -> None:
        for uid in self.app.layout.users():
            for row in self.app.log_for(uid).voice_pending():
                entry = self.app.log_for(uid).entry(row["message_id"])
                filename = "video_note.mp4" if entry is not None and entry.kind == "video_note" else "voice.ogg"
                self.running.start(uid, row["message_id"], self.run(uid, row["message_id"], filename))

    async def run(self, uid: int, message_id: int, filename: str) -> None:
        store = self.app.log_for(uid)
        key = self.app.config.elevenlabs_key
        row = store.conn.execute(
            "SELECT file_id, attempts FROM voice_jobs WHERE message_id = ? AND status = 'pending'", (message_id,)
        ).fetchone()
        if row is None or not key:
            return
        try:
            terms = filter_terms(Facts(self.app.layout.facts_path(uid)).keyterms())
        except FactsUnavailable as exc:  # the transcript is worth more than the hints
            log.warning("user %d: facts.sqlite unavailable (%s); transcribing without keyterms", uid, exc.reason)
            terms = []
        raw: str | None = None
        attempts = int(row["attempts"])
        async with self._slots:
            while raw is None:
                if attempts >= MAX_ATTEMPTS:
                    return
                try:
                    data = await download(self.app.bot, row["file_id"])
                    raw = await transcribe(self.client, key, data, filename, terms)
                    if not raw:
                        raise ScribeError("empty")
                # Scribe, the Bot API or the network. The file URL carries the bot token, so only a ScribeError
                # (whose message is ours) is logged with its text; anything else by its type alone.
                except Exception as exc:
                    raw = None
                    attempts += 1
                    reason = f"{type(exc).__name__}: {exc}" if isinstance(exc, ScribeError) else type(exc).__name__
                    final = attempts >= MAX_ATTEMPTS
                    store.voice_set(message_id, "failed" if final else "pending", error=reason, attempt=True)
                    if final:
                        log.warning("user %d: voice %d not transcribed: %s", uid, message_id, reason)
                        return
                    await self.sleep(BACKOFF_S[attempts - 1])
            cleaned, ok = await self._clean(uid, raw, terms)
        store.record_transcript(message_id, raw, cleaned.text, ok, self.app.clock())
        store.voice_set(message_id, "done")
        await self._post(uid, message_id, cleaned.text)
        await self.app.on_transcript(uid, message_id)  # REQ-VOICE-7: now the note is text, and asked as text is

    async def _clean(self, uid: int, raw: str, terms: list[str]) -> tuple[Cleaned, bool]:
        """The cleaned transcript and whether the cleaning call succeeded (REQ-VOICE-5: raw beats none)."""
        provider = self.agent.provider if self.agent is not None else None
        if provider is None:
            return Cleaned(raw), False
        try:
            return await clean(raw, provider=provider, env=self.env(), keyterms=terms), True
        except (TelevibeError, CleanupError) as exc:
            log.warning("user %d: transcript cleanup failed: %s", uid, type(exc).__name__)
            return Cleaned(raw), False

    async def _post(self, uid: int, message_id: int, text: str) -> bool:
        """REQ-VOICE-6: the clean text as quote messages replying to the voice note, each a bot_sent."""
        for part in chunks(text):
            try:
                sent = await self.app.bot.send_message(
                    chat_id=uid,
                    text=part,
                    entities=quote(part),
                    parse_mode=None,
                    reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=True),
                )
            except (TelegramAPIError, OSError, TimeoutError) as exc:
                log.warning("user %d: transcript not sent: %s", uid, type(exc).__name__)
                return False
            self.app.log_for(uid).record_bot_sent(
                sent.message_id, part, message_id, sent.model_dump(mode="json", exclude_none=True), self.app.clock()
            )
        return True
