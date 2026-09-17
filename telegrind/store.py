"""Message, entry and fact repository.

The log is append-on-first-sight, overwrite-on-edit. Nothing here deletes a
message row: a Telegram delete removes facts, never the log. An entry is
written together with its message, in the same transaction, and nothing
here deletes an entry either.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram.types import Message
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    SOURCE_TELEGRAM,
    VERDICT_FACT,
    Chat,
    Entry,
    Fact,
    LoggedMessage,
)

if TYPE_CHECKING:  # `extract` imports `store`; the cycle stays a type concern.
    from telegrind.extract import Draft


def message_kind(msg: Message) -> str:
    return KIND_VOICE if getattr(msg, "voice", None) else KIND_TEXT


def edited_at(msg: Message) -> datetime | None:
    """The edit timestamp, as a datetime.

    aiogram parses `date` into a datetime but leaves `edit_date` a raw
    Unix int, and the column is a timestamptz — so handing it straight
    over kills the whole edit with a DataError. Telegram's timestamps are
    UTC.
    """
    raw = getattr(msg, "edit_date", None)
    if raw is None or isinstance(raw, datetime):
        return raw
    return datetime.fromtimestamp(raw, tz=UTC)


def message_values(msg: Message) -> dict[str, Any]:
    """Lift the log columns off an aiogram message.

    A forwarded message's own date is the date of the forwarded content,
    which is what `services/expense.py` used too — a forward of last week's
    receipt should not be dated today.
    """
    kind = message_kind(msg)
    voice = getattr(msg, "voice", None)
    origin = getattr(msg, "forward_origin", None)

    return {
        "message_id": msg.message_id,
        "kind": kind,
        "text": None if kind == KIND_VOICE else (msg.text or msg.caption),
        "transcript": None,
        "transcript_model": None,
        "audio_file_id": voice.file_id if voice else None,
        "audio_duration": voice.duration if voice else None,
        "tg_date": origin.date if origin else msg.date,
        "edited_at": edited_at(msg),
        "raw": msg.model_dump(mode="json"),
    }


async def get_message(
    session: AsyncSession, chat_pk: int, message_id: int
) -> LoggedMessage | None:
    result = await session.execute(
        select(LoggedMessage).where(
            LoggedMessage.chat_pk == chat_pk,
            LoggedMessage.message_id == message_id,
        )
    )
    return result.scalar_one_or_none()


def reply_to(row: LoggedMessage) -> int | None:
    """The Telegram message_id this row replies to, if any.

    Read off `raw` rather than off a live aiogram object because Telegram
    does not nest replies: `reply_to_message.reply_to_message` is always
    None, so the second hop of a session lookup has to come from our own
    stored copy of the parent.
    """
    return ((row.raw or {}).get("reply_to_message") or {}).get("message_id")


def by_the_bot(row: LoggedMessage) -> bool:
    """Did the bot itself put this message in the chat?

    `raw` carries aiogram's own field name `from_user`, never the Bot
    API's `from`: `upsert_message` dumps without `by_alias`, so the alias
    never survives into the column.
    """
    return bool(((row.raw or {}).get("from_user") or {}).get("is_bot"))


async def turn_root(session: AsyncSession, chat_pk: int, message_id: int) -> int:
    """Which message a reply to `message_id` continues.

    The host half of `meta.sessions.turn_key`, and it has to agree with
    it. They answer two halves of one question — which verdict a reply
    inherits, and which session its turn writes to — so a chain where
    they disagree hands Claude a message in a session that never saw the
    thread: an answer with no memory of the conversation it is in, which
    is the failure this resolution exists to prevent. One test feeds the
    same chain to both.

    Total by construction: an unknown parent is its own root, so a
    conversation that predates the bot storing what it says degrades to
    «continues nothing» rather than raising.
    """
    parent = await get_message(session, chat_pk, message_id)
    if parent is None or not by_the_bot(parent):
        return message_id
    return reply_to(parent) or message_id


def _entry_for(chat: Chat, row: LoggedMessage, verdict: str) -> Entry:
    """A fresh entry for a message row that has just been written.

    `content` duplicates what the extractor reads, so the queue is a single
    indexed read on one table rather than a join that would drop every
    side-loaded entry. There is exactly one writer of that duplicate — this
    module — and it is verified: nothing writes `transcript` anywhere yet.
    **A future transcription path must update `entry.content` and clear
    `entry.extracted_at` in the same transaction as the transcript**, or a
    voice entry goes stale and the queue skips it with no trace.
    """
    return Entry(
        chat_pk=chat.id,
        source=SOURCE_TELEGRAM,
        external_id=str(row.message_id),
        message_pk=row.id,
        occurred_at=row.tg_date,
        content=row.content or None,
        verdict=verdict,
    )


async def get_entry_for_message(session: AsyncSession, message_pk: int) -> Entry | None:
    result = await session.execute(select(Entry).where(Entry.message_pk == message_pk))
    return result.scalar_one_or_none()


async def upsert_message(
    session: AsyncSession,
    chat: Chat,
    msg: Message,
    *,
    verdict: str = VERDICT_FACT,
) -> tuple[LoggedMessage, Entry, bool]:
    """Append the message and its entry, or overwrite both on an edit.

    Returns `(message, entry, created)`. One transaction writes both rows,
    which is what keeps the ingest invariant: the row is committed before
    any model call, and an entry that exists is an entry the queue can see.

    An edit overwrites the text, bumps edited_at, and clears the entry's
    extraction state: the text changed, so whatever was extracted from it no
    longer describes it, and clearing extracted_at is what makes the next
    batch pass pick it up again.
    """
    values = message_values(msg)
    existing = await get_message(session, chat.id, msg.message_id)
    if existing is not None:
        # Never clobber a stored transcript with None on a text edit.
        for key, value in values.items():
            if key in ("transcript", "transcript_model") and value is None:
                continue
            setattr(existing, key, value)
        entry = await get_entry_for_message(session, existing.id)
        if entry is None:
            # A message with no entry cannot happen: every writer here makes
            # both. Healing beats raising anyway — a NoResultFound inside
            # `record`'s transaction would roll the message write back and
            # lose the update, which is the one thing this bot promises not
            # to do.
            entry = _entry_for(chat, existing, verdict)
            session.add(entry)
            await session.flush()
            return existing, entry, False
        entry.occurred_at = existing.tg_date
        entry.content = existing.content or None
        entry.verdict = verdict
        entry.extracted_at = None
        entry.extract_error = None
        return existing, entry, False

    row = LoggedMessage(chat_pk=chat.id, **values)
    session.add(row)
    await session.flush()
    entry = _entry_for(chat, row, verdict)
    session.add(entry)
    await session.flush()
    return row, entry, True


def _has_content() -> Any:
    """SQL for «this entry has something the extractor can read».

    A photo, a sticker or a location is stored like everything else and
    simply waits. A structured entry has no content at all and is excluded
    here as well as by its extraction stamp — two independent reasons, so
    neither has to be trusted alone.
    """
    return func.nullif(func.trim(Entry.content), "").is_not(None)


async def unextracted_tail(
    session: AsyncSession, chat_pk: int, *, limit: int = 200
) -> list[Entry]:
    """The entries a pass is responsible for, oldest first.

    `entry` alone, never joined to `message`: a join would drop every
    side-loaded entry, which is the whole point of the table.
    """
    result = await session.execute(
        select(Entry)
        .where(
            Entry.chat_pk == chat_pk,
            Entry.verdict == VERDICT_FACT,
            Entry.extracted_at.is_(None),
            _has_content(),
        )
        .order_by(Entry.occurred_at, Entry.id)
        .limit(limit)
    )
    return list(result.scalars())


async def context_before(
    session: AsyncSession,
    chat_pk: int,
    pivot: Entry,
    *,
    limit: int = 10,
) -> list[Entry]:
    """Read-only neighbours shown to the model but never re-extracted.

    Ordered by when things happened, not by `id`: a forward is dated by its
    origin and an imported row by its own date, so the two genuinely differ.
    The comparison is a row comparison so that two entries sharing a second
    still order deterministically.
    """
    result = await session.execute(
        select(Entry)
        .where(
            Entry.chat_pk == chat_pk,
            _has_content(),
            tuple_(Entry.occurred_at, Entry.id) < (pivot.occurred_at, pivot.id),
        )
        .order_by(Entry.occurred_at.desc(), Entry.id.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def messages_for(
    session: AsyncSession, entries: list[Entry]
) -> dict[int, LoggedMessage]:
    """The message rows behind whichever entries have one, keyed by entry id.

    One explicit query, never an attribute on a relationship: a lazy load on
    an AsyncSession raises MissingGreenlet at the attribute access, which
    mentions neither commits nor transactions, and the fake sessions in this
    suite cannot see it.
    """
    by_message_pk = {e.message_pk: e.id for e in entries if e.message_pk is not None}
    if not by_message_pk:
        return {}
    result = await session.execute(
        select(LoggedMessage).where(LoggedMessage.id.in_(by_message_pk))
    )
    return {by_message_pk[row.id]: row for row in result.scalars()}


async def live_facts_for_entry(session: AsyncSession, entry_pk: int) -> list[Fact]:
    """This entry's facts that are not tombstoned."""
    result = await session.execute(
        select(Fact)
        .where(Fact.entry_pk == entry_pk, Fact.deleted_at.is_(None))
        .order_by(Fact.seq)
    )
    return list(result.scalars())


async def tombstone_facts(session: AsyncSession, entry_pk: int, at: datetime) -> int:
    """Soft-delete this entry's live facts. Returns how many were stamped.

    A tombstone is never lifted by an extraction pass — only restore_facts
    clears it — because facts are re-derivable and a hard delete would be
    undone by the next re-extraction of the same entry.
    """
    result = await session.execute(
        select(Fact).where(Fact.entry_pk == entry_pk, Fact.deleted_at.is_(None))
    )
    rows = list(result.scalars())
    for row in rows:
        row.deleted_at = at
    return len(rows)


async def restore_facts(session: AsyncSession, entry_pk: int) -> int:
    """Clear the tombstone on this entry's facts. Returns how many."""
    result = await session.execute(
        select(Fact).where(Fact.entry_pk == entry_pk, Fact.deleted_at.is_not(None))
    )
    rows = list(result.scalars())
    for row in rows:
        row.deleted_at = None
    return len(rows)


def mark_extracted(
    rows: list[Entry], *, model: str, prompt_version: str, at: datetime
) -> None:
    """An entry the pass handled, whether or not it yielded a fact.

    Marking the silent ones is the whole point of the column: without it
    «привет» is indistinguishable from «not yet parsed» and every pass
    re-feeds it forever.
    """
    for row in rows:
        row.extracted_at = at
        row.extract_model = model
        row.extract_prompt_version = prompt_version
        row.extract_error = None


def mark_failed(rows: list[Entry], error: str) -> None:
    """The pass could not read these. They stay pending and are retried.

    Dropping the echo removed the only channel through which a failure
    reached the user, so it has to be countable here instead.
    """
    for row in rows:
        row.extract_error = error


async def replace_facts(
    session: AsyncSession,
    chat_pk: int,
    entry_pk: int,
    drafts: list[Draft],
    *,
    model: str,
    prompt_version: str,
    now: datetime,
) -> int:
    """Diff this entry's facts against what the pass just derived.

    Unchanged rows are left alone, changed ones updated in place, surplus
    ones tombstoned. A tombstone is never lifted here — only the user's
    reaction clears `deleted_at` — which is why inserting over a
    tombstoned `(entry_pk, seq)` has to work, and why the uniqueness on
    it is a partial index.
    """
    live = {row.seq: row for row in await live_facts_for_entry(session, entry_pk)}

    for draft in drafts:
        row = live.pop(draft.seq, None)
        if row is None:
            session.add(
                Fact(
                    chat_pk=chat_pk,
                    entry_pk=entry_pk,
                    seq=draft.seq,
                    kind=draft.kind,
                    at=draft.at,
                    fields=draft.fields,
                    model=model,
                    prompt_version=prompt_version,
                )
            )
            continue
        row.kind = draft.kind
        row.at = draft.at
        row.fields = draft.fields
        row.model = model
        row.prompt_version = prompt_version

    for surplus in live.values():
        surplus.deleted_at = now

    return len(drafts)
