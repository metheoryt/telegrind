"""One-time import of the v1 history from a Telegram Desktop export.

Run from a shell, not from the chat. Every decision it makes is a fact
about the export entry — who sent it, whether it starts with a slash —
rather than a guess, which is why `classify` is never called: the
classifier exists because a live message arrives without that structure,
and 3915 model calls would buy nothing here.

Deleted or frozen once it has run. It is one file for the same reason.
"""

import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram.types import Chat as TgChat
from aiogram.types import Message, User, Voice
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import extract, store
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, VERDICT_SYSTEM, Chat

log = logging.getLogger(__name__)

#: v1's delete marker. 56 of them in the corpus.
DASH = "-"


def flatten(raw: Any) -> str:
    """The export's `text`, as a string.

    Telegram Desktop writes a plain string for unformatted text and a
    list of fragments — strings and `{"type": ..., "text": ...}` dicts —
    for anything with a link, a bold run or a code span. 350 entries in
    this corpus take the second shape, so it is not an edge case.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    return "".join(
        part if isinstance(part, str) else str(part.get("text", "")) for part in raw
    )


def user_id(entry: dict) -> int | None:
    """The author's Telegram id, or None if the entry has no user author.

    `from_id` is `user<N>` for a person or a bot; a channel post reads
    `channel<N>` and a service entry has no `from_id` at all.
    """
    raw = entry.get("from_id")
    if not isinstance(raw, str) or not raw.startswith("user"):
        return None
    return int(raw.removeprefix("user"))


def verdict_of(entry: dict, *, bot_id: int) -> str:
    """What this entry is, decided from its structure.

    Four arms, matching `telegrind/classify.py`'s vocabulary. `talk` is
    not among them: the classifier reaches it for a reply into a
    conversation, and 57 of the user's 4069 messages are replies, none of
    them to the bot — so asserting `fact` is wrong zero times here.
    """
    if user_id(entry) == bot_id:
        return VERDICT_SYSTEM
    text = flatten(entry.get("text")).strip()
    if text == DASH:
        return VERDICT_SYSTEM
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@")[0]
        return VERDICT_QUESTION if command == "/q" else VERDICT_SYSTEM
    return VERDICT_FACT


def _stub_reply(message_id: int, *, chat_id: int, date: datetime) -> Message:
    """The parent, as much of it as anything ever reads.

    `store.reply_to` reads `raw["reply_to_message"]["message_id"]` and
    nothing else — Telegram does not nest a second hop, which is why the
    column is read rather than the object. A stub carrying the id is
    therefore complete, not partial. Its date is the child's: an aiogram
    `Message` requires one, and no reader of this object looks at it.
    """
    return Message(
        message_id=message_id, date=date, chat=TgChat(id=chat_id, type="private")
    )


def message_from(entry: dict, *, chat_id: int, bot_id: int) -> Message | None:
    """One export entry as the aiogram object live ingestion would see.

    Returns None for anything that is not a user-or-bot message: the two
    service entries, and any future shape with no `user<N>` author.

    Built as an aiogram object rather than as column values because
    `store.message_values` dumps it into `raw`, and three readers parse
    that column afterwards — `store.by_the_bot`, `store.reply_to` and
    `extract.author_of`. Going through the same type is what makes the
    shape identical by construction instead of by agreement.
    """
    if entry.get("type") != "message":
        return None
    author = user_id(entry)
    if author is None:
        return None

    date = datetime.fromtimestamp(int(entry["date_unixtime"]), tz=UTC)
    edited = entry.get("edited_unixtime")
    reply = entry.get("reply_to_message_id")

    voice = None
    if entry.get("media_type") == "voice_message":
        # The export ships the audio but not Telegram's file id, and
        # `aiogram.types.Voice` requires one. The `import:` prefix is the
        # signal to whatever wires transcription later that this id
        # cannot be fetched — a placeholder that looks fetchable is worse
        # than an obvious one.
        voice = Voice(
            file_id=f"import:{entry.get('file', '')}",
            file_unique_id=f"import:{entry['id']}",
            duration=int(entry.get("duration_seconds") or 0),
        )

    return Message(
        message_id=int(entry["id"]),
        date=date,
        # The raw int, not a datetime: `message_values` dumps with
        # mode="json", where a datetime becomes an ISO string and the live
        # path leaves an int.
        edit_date=int(edited) if edited else None,
        chat=TgChat(id=chat_id, type="private"),
        from_user=User(
            id=author,
            is_bot=author == bot_id,
            first_name=str(entry.get("from") or "?"),
        ),
        text=flatten(entry.get("text")) or None,
        voice=voice,
        reply_to_message=(
            _stub_reply(int(reply), chat_id=chat_id, date=date) if reply else None
        ),
    )


#: The only export type this reads. A group export has many authors and
#: no per-chat meaning for `chat_id`.
BOT_CHAT = "bot_chat"


# Doesn't end in "Error": ExportMismatch is the name this task's interface,
# and the tests that import it, require.
class ExportMismatch(Exception):  # noqa: N818
    """The export is not the one chat this import is for."""


@dataclass(frozen=True, slots=True)
class Export:
    bot_id: int
    entries: list[dict]


def read_export(path: Path, *, chat_id: int, since: datetime | None = None) -> Export:
    """The export's message entries, and who the bot is.

    Refuses rather than guesses. A third author means this is not the
    two-party chat the caller named, and importing it would file someone
    else's messages under `chat_id`.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("type") != BOT_CHAT:
        raise ExportMismatch(f"not a bot chat export: type={doc.get('type')!r}")

    bot_id = int(doc["id"])
    entries = [e for e in doc["messages"] if e.get("type") == "message"]

    authors: set[int] = {a for e in entries if (a := user_id(e)) is not None}
    unexpected = authors - {bot_id, chat_id}
    if unexpected:
        raise ExportMismatch(
            f"unexpected authors {sorted(unexpected)}; expected only "
            f"{chat_id} and {bot_id}"
        )

    if since is not None:
        cutoff = since.timestamp()
        entries = [e for e in entries if float(e["date_unixtime"]) >= cutoff]

    return Export(bot_id=bot_id, entries=entries)


#: Rows per transaction. Small enough that a failure costs one chunk,
#: large enough that 6835 entries are not 6835 commits.
CHUNK = 500


@dataclass(frozen=True, slots=True)
class ImportReport:
    seen: int
    stored: int
    skipped: int
    forwards: int
    verdicts: Counter


def _classify(
    item: dict, *, chat_id: int, bot_id: int
) -> tuple[Message | None, str | None, bool]:
    """One entry's export-level facts: what it becomes, and nothing about
    the write.

    Returns `(message, verdict, is_forward)`. `message` is `None` exactly
    when the entry is skipped, and `verdict` is `None` in step with it.
    `is_forward` reflects a truthy `forwarded_from` regardless of whether
    the entry is otherwise skipped — R6 counts a forward as a fact about
    the export, not about the write.

    This is the single seam `import_entries` reads `seen`, `forwards`,
    `skipped` and `verdicts` from, on both the dry-run and the real path:
    those four describe the export, never the write, so a dry run and a
    real run over the same entries must count all four identically. Only
    whether to call `upsert` — and therefore `stored` — may differ between
    the two branches below.
    """
    is_forward = bool(item.get("forwarded_from"))
    msg = message_from(item, chat_id=chat_id, bot_id=bot_id)
    verdict = None if msg is None else verdict_of(item, bot_id=bot_id)
    return msg, verdict, is_forward


async def import_entries(
    session: AsyncSession,
    chat: Chat,
    export: Export,
    *,
    upsert: Callable[..., Awaitable[Any]] = store.upsert_message,
    chunk: int = CHUNK,
    dry_run: bool = False,
) -> ImportReport:
    """Store every entry, one transaction per chunk.

    Nothing here routes, reacts or replies. Going through the dispatcher
    would put a 💔 on three thousand historical messages and answer every
    question in the log into a live chat.

    Idempotent by construction: `upsert_message` overwrites on
    `(chat_pk, message_id)` and clears `extracted_at`, so a re-run
    re-queues exactly what changed.

    `forwards` counts every entry carrying a truthy `forwarded_from`, in
    both the dry-run and the real path: it is a fact about the export, not
    about the write. A forward loses its origin date and, because
    `forward_origin` is never set on the reconstructed `Message`, reaches
    the extractor's prompt as though the words were the chat's own author's
    — neither is reconstructable from the export, so counting is the whole
    remedy. See `_classify` for the shared seam that keeps this true.
    """
    seen = stored = skipped = forwards = 0
    verdicts: Counter = Counter()

    for start in range(0, len(export.entries), chunk):
        batch = export.entries[start : start + chunk]
        if dry_run:
            for item in batch:
                seen += 1
                msg, verdict, is_forward = _classify(
                    item, chat_id=chat.chat_id, bot_id=export.bot_id
                )
                if is_forward:
                    forwards += 1
                if msg is None:
                    skipped += 1
                else:
                    verdicts[verdict] += 1
            continue

        async with session.begin():
            for item in batch:
                seen += 1
                msg, verdict, is_forward = _classify(
                    item, chat_id=chat.chat_id, bot_id=export.bot_id
                )
                if is_forward:
                    forwards += 1
                if msg is None:
                    skipped += 1
                    continue
                await upsert(session, chat, msg, verdict=verdict)
                verdicts[verdict] += 1
                stored += 1

        log.info("imported %s/%s entries", seen, len(export.entries))

    return ImportReport(
        seen=seen,
        stored=stored,
        skipped=skipped,
        forwards=forwards,
        verdicts=verdicts,
    )


#: Messages per extraction pass. `extract._pass` makes one model call for
#: the whole tail and `llm.MAX_TOKENS` is 2048 — roughly forty facts —
#: so this is a correctness bound, not a throughput knob. The default
#: limit of 200 would truncate the reply and fail the batch whole.
BATCH = 20


async def extract_all(
    session: AsyncSession,
    chat: Chat,
    *,
    batch: int = BATCH,
    max_passes: int | None = None,
    run: Callable[..., Awaitable[extract.Report]] = extract.run,
) -> list[extract.Report]:
    """Drive passes until the tail is empty, one transaction each.

    Stops on the first failed pass rather than retrying: `_pass` leaves a
    failed tail pending, so a loop that continued would ask the same
    twenty messages again until the money ran out.
    """
    cfg = ChatConfig.of(chat)
    reports: list[extract.Report] = []

    while max_passes is None or len(reports) < max_passes:
        async with session.begin():
            report = await run(session, chat, cfg, limit=batch)
        reports.append(report)
        log.info(
            "pass %s: extracted=%s facts=%s failed=%s complaints=%s",
            len(reports),
            report.extracted,
            report.facts,
            report.failed,
            report.complaints,
        )
        if report.failed or report.pending == 0:
            break

    return reports
