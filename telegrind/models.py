from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

KIND_TEXT = "text"
KIND_VOICE = "voice"

ORIGIN_EXTRACTED = "extracted"
ORIGIN_IMPORTED = "imported"


class Model(AsyncAttrs, DeclarativeBase):
    pass


class Chat(Model):
    __tablename__ = "chat"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_chat_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column("chat_id", BigInteger)
    sheet_url: Mapped[str | None]


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

    @property
    def content(self) -> str:
        """What extraction reads."""
        return self.transcript or self.text or ""


class Fact(Model):
    """A replaceable derivation of a LoggedMessage — or of an imported row.

    Facts are chat-scoped through chat_pk rather than only through the
    message, because an imported fact has no message: its source text was
    never logged.
    """

    __tablename__ = "fact"
    __table_args__ = (
        UniqueConstraint("message_pk", "seq", name="uq_fact_message_pk_seq"),
        UniqueConstraint(
            "chat_pk", "worksheet", "sheet_key", name="uq_fact_chat_pk_worksheet_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE")
    )
    #: 1-based position within the message.
    seq: Mapped[int]
    category: Mapped[str]
    #: {header: coerced value} — exactly what projection writes.
    fields: Mapped[dict] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(default=ORIGIN_EXTRACTED)
    model: Mapped[str | None]
    prompt_version: Mapped[str | None]
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    worksheet: Mapped[str]
    #: What sits in column A: "<telegram message_id>_<seq>".
    sheet_key: Mapped[str]
