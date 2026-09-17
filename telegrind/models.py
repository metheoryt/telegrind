from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

KIND_TEXT = "text"
KIND_VOICE = "voice"

#: What the classifier decided a message is, and therefore what happens to
#: it. Never null: a null would mean «not classified yet», «the classifier
#: failed» and «the classifier said it is not a fact» all at once, and the
#: flag would be undebuggable exactly when it misroutes. A classifier
#: failure writes VERDICT_FACT explicitly, which is the behaviour that
#: shipped before the classifier existed.
VERDICT_FACT = "fact"
VERDICT_QUESTION = "question"
VERDICT_TALK = "talk"
#: Rows the classifier never sees: every message the bot or Claude sends,
#: and every slash command that is not /q.
VERDICT_SYSTEM = "system"
VERDICTS = (VERDICT_FACT, VERDICT_QUESTION, VERDICT_TALK, VERDICT_SYSTEM)

#: Where an entry came from. A string, not an FK: a table earns its place
#: when sources acquire configuration (per-bank column mappings), and
#: promoting a string to an FK later is a migration, not a redesign.
SOURCE_TELEGRAM = "telegram"


class Model(AsyncAttrs, DeclarativeBase):
    pass


class Chat(Model):
    __tablename__ = "chat"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_chat_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column("chat_id", BigInteger)
    sheet_url: Mapped[str | None]
    #: Hours east of UTC. A fixed offset, not a zone name: the bot serves one
    #: person per chat and DST has never come up. Default is Almaty.
    tz_offset: Mapped[int] = mapped_column(default=6, server_default="6")
    #: ISO 4217, used when a message states an amount and no currency.
    currency: Mapped[str] = mapped_column(default="KZT", server_default="KZT")


class File(Model):
    __tablename__ = "file"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str]
    filename: Mapped[str]


class LoggedMessage(Model):
    """The log. Appended on first sight, overwritten on a Telegram edit.

    Named LoggedMessage, not Message: `aiogram.types.Message` is imported in
    middleware.py, start.py and handlers.py, and a shadowed name there is a
    silent bug.
    """

    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("chat_pk", "message_id", name="uq_message_chat_pk_message_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: FK to chat.id — the surrogate PK, NOT the Telegram chat_id.
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    #: Telegram's own message id.
    message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str]
    text: Mapped[str | None]
    transcript: Mapped[str | None]
    transcript_model: Mapped[str | None]
    audio_file_id: Mapped[str | None]
    audio_duration: Mapped[int | None]
    tg_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    #: The reaction the bot last placed on this message. There is no API
    #: to read a message's reactions back, so the receipt has to remember
    #: itself; an edit advances it, which is the only signal that the bot
    #: noticed the edit at all.
    receipt_emoji: Mapped[str | None] = mapped_column(default=None)

    @property
    def content(self) -> str:
        """What extraction reads."""
        return self.transcript or self.text or ""


class Entry(Model):
    """The unit that yields facts. A Telegram message is one kind of entry.

    The log was built on the assumption that a fact is derived from a
    message, and that assumption is wrong in two directions: the v1
    workbook is already structured, and receipts and bank statements are
    coming. So the derivation state that used to sit on `message` — the
    verdict and the four extraction columns — sits here instead, and
    `message` goes back to being the verbatim record of what Telegram sent.

    `source` is also the undo. Everything one import wrote is
    `WHERE source = '<that source>'`, which is why no import-run record
    exists.
    """

    __tablename__ = "entry"
    __table_args__ = (
        UniqueConstraint(
            "chat_pk", "source", "external_id", name="uq_entry_chat_source_external"
        ),
        #: Partial, because most entries have no message: Postgres treats
        #: NULLs as distinct, so a total unique index would admit any number
        #: of message-less entries and then mean nothing.
        Index(
            "uq_entry_message_pk",
            "message_pk",
            unique=True,
            postgresql_where=text("message_pk IS NOT NULL"),
        ),
        Index("ix_entry_queue", "chat_pk", "verdict", "extracted_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    source: Mapped[str]
    #: The source's own key, as text. Telegram: `str(message_id)`. A sheet
    #: row: its column-A key. A statement: the transaction id.
    external_id: Mapped[str]
    #: Set only for chat-borne entries. Who wrote a message and what it
    #: replies to are Telegram facts, read through here rather than copied.
    message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), default=None
    )
    #: When the thing happened: `tg_date` for a message, the row's own date
    #: for an import.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: What the extractor reads. NULL for a structured entry, which yielded
    #: its facts on arrival and needs no model.
    content: Mapped[str | None] = mapped_column(default=None)
    #: The source row verbatim, for imported entries; NULL for chat entries,
    #: whose verbatim copy is `message.raw`. Deliberately never shown to the
    #: model — `taxonomy.observed` reads `fact.fields` only — so this is
    #: where a source's columns go when they must be preserved but must not
    #: enter the extractor's vocabulary.
    raw: Mapped[dict | None] = mapped_column(JSONB, default=None)
    #: What routing decided this is, and therefore what happens to it. Never
    #: null, for the reasons written at VERDICTS. It lives here rather than
    #: on `message` because the extraction state lives here: two flags that
    #: can disagree about whether something gets extracted is the defect
    #: `4d60c7b65ad2` removed.
    verdict: Mapped[str] = mapped_column(default=VERDICT_FACT, server_default="fact")
    extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    extract_model: Mapped[str | None] = mapped_column(default=None)
    extract_prompt_version: Mapped[str | None] = mapped_column(default=None)
    extract_error: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Fact(Model):
    """A replaceable derivation of an Entry.

    Service columns plus JSONB. Only `kind` and `at` are promoted out of
    `fields`, because every query filters on both. The numeric shape is
    deliberately not promoted: expenses are flows, measurements are levels,
    habits have no number, assets have a balance. Promoting later is a
    generated column, not a rewrite.
    """

    __tablename__ = "fact"
    __table_args__ = (
        Index(
            "uq_fact_entry_pk_seq_live",
            "entry_pk",
            "seq",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_fact_chat_kind_at_live",
            "chat_pk",
            "kind",
            "at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_fact_fields", "fields", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    entry_pk: Mapped[int] = mapped_column(ForeignKey("entry.id", ondelete="CASCADE"))
    #: 1-based position within the entry.
    seq: Mapped[int]
    #: Free-form, coined by the model and reused through the observed
    #: taxonomy. There is no registry of permitted values.
    kind: Mapped[str]
    #: When the fact HAPPENED, which is not created_at. Falls back to the
    #: entry's occurred_at when the text states no time of its own.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Everything else. Numbers in here are real JSON numbers — see
    #: telegrind/coerce.py, which is the only place that writes them.
    fields: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(default=None)
    prompt_version: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    #: A tombstone, and not for undo. Facts are re-derivable, so a hard
    #: delete is undone by the next re-extraction of the same message.
    #: Only an explicit un-delete by the user clears this.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
