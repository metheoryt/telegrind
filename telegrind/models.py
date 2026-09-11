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
    #: When the batch pass last extracted this message. Null means it has
    #: not been extracted yet — which is NOT the same as "extracted and
    #: yielded nothing", and that difference is why this column exists.
    extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    extract_model: Mapped[str | None] = mapped_column(default=None)
    extract_prompt_version: Mapped[str | None] = mapped_column(default=None)
    #: False for /q, for commands, and for imported bot replies. Such a
    #: message is stored like any other — nothing written is ever lost —
    #: but it must never reach the extractor, or the batch pass coins a
    #: kind out of a question and poisons the observed taxonomy.
    extractable: Mapped[bool] = mapped_column(default=True, server_default="true")
    #: What routing decided this message is. Derived, like extracted_at,
    #: and re-derived on an edit. It replaces `extractable` as the thing
    #: the tail is selected by; `extractable` is still written in step
    #: with it, and dropping that column is a later contract step.
    verdict: Mapped[str] = mapped_column(default=VERDICT_FACT, server_default="fact")
    #: The last extraction failure. Without it, dropping the echo would
    #: make a failed extraction completely silent.
    extract_error: Mapped[str | None] = mapped_column(default=None)
    #: The reaction the bot last placed on this message. There is no API
    #: to read a message's reactions back, so the receipt has to remember
    #: itself; an edit advances it, which is the only signal that the bot
    #: noticed the edit at all.
    receipt_emoji: Mapped[str | None] = mapped_column(default=None)

    @property
    def content(self) -> str:
        """What extraction reads."""
        return self.transcript or self.text or ""


class Fact(Model):
    """A replaceable derivation of a LoggedMessage.

    Service columns plus JSONB. Only `kind` and `at` are promoted out of
    `fields`, because every query filters on both. The numeric shape is
    deliberately not promoted: expenses are flows, measurements are levels,
    habits have no number, assets have a balance. Promoting later is a
    generated column, not a rewrite.
    """

    __tablename__ = "fact"
    __table_args__ = (
        Index(
            "uq_fact_message_pk_seq_live",
            "message_pk",
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
    message_pk: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE")
    )
    #: 1-based position within the message.
    seq: Mapped[int]
    #: Free-form, coined by the model and reused through the observed
    #: taxonomy. There is no registry of permitted values.
    kind: Mapped[str]
    #: When the fact HAPPENED, which is not created_at. Falls back to the
    #: message's tg_date when the text states no time of its own.
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
