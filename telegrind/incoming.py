"""What a Telegram message is, lifted into the log's columns (SPEC section 3, REQ-MSG-1, REQ-MSG-4)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram.types import Message, MessageOriginChannel, MessageOriginChat, MessageOriginHiddenUser, MessageOriginUser

KINDS = ("text", "voice", "video_note", "photo", "document", "sticker", "other")
AUTHOR_ME = "me"
AUTHOR_BOT = "bot"


@dataclass(frozen=True, slots=True)
class Media:
    file_id: str
    name: str | None
    mime: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class Voice:
    file_id: str
    duration: int
    filename: str
    size: int | None


@dataclass(frozen=True, slots=True)
class Incoming:
    message_id: int
    author: str
    kind: str
    text: str | None
    occurred_at: int
    received_at: int
    edited_at: int | None
    reply_to: int | None
    raw: dict[str, Any]
    media: Media | None
    voice: Voice | None


def _seconds(value: datetime | int | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp()) if isinstance(value, datetime) else int(value)


def author_of(msg: Message) -> str:
    origin = msg.forward_origin
    if isinstance(origin, MessageOriginUser):
        return f"forward: {origin.sender_user.full_name}"
    if isinstance(origin, MessageOriginHiddenUser):
        return f"forward: hidden {origin.sender_user_name}"
    if isinstance(origin, MessageOriginChat):
        return f"forward: {origin.sender_chat.title or origin.sender_chat.id}"
    if isinstance(origin, MessageOriginChannel):
        return f"forward: channel {origin.chat.title or origin.chat.id}"
    if msg.from_user is not None and msg.from_user.is_bot:
        return AUTHOR_BOT
    return AUTHOR_ME


def kind_of(msg: Message) -> str:
    if msg.voice:
        return "voice"
    if msg.video_note:
        return "video_note"
    if msg.photo:
        return "photo"
    if msg.document:
        return "document"
    if msg.sticker:
        return "sticker"
    if msg.text is not None:
        return "text"
    return "other"


def media_of(msg: Message) -> Media | None:
    if msg.photo:
        best = max(msg.photo, key=lambda p: (p.file_size or 0, p.width * p.height))
        return Media(best.file_id, "photo.jpg", "image/jpeg", best.file_size)
    if msg.document:
        d = msg.document
        return Media(d.file_id, d.file_name, d.mime_type, d.file_size)
    return None


def voice_of(msg: Message) -> Voice | None:
    if msg.voice:
        return Voice(msg.voice.file_id, msg.voice.duration, "voice.ogg", msg.voice.file_size)
    if msg.video_note:
        return Voice(msg.video_note.file_id, msg.video_note.duration, "video_note.mp4", msg.video_note.file_size)
    return None


def incoming(msg: Message, *, now: int | None = None) -> Incoming:
    occurred = msg.forward_origin.date if msg.forward_origin is not None else msg.date
    return Incoming(
        message_id=msg.message_id,
        author=author_of(msg),
        kind=kind_of(msg),
        text=msg.text if msg.text is not None else msg.caption,
        occurred_at=_seconds(occurred) or int(time.time()),
        received_at=now if now is not None else int(time.time()),
        edited_at=_seconds(msg.edit_date),
        reply_to=msg.reply_to_message.message_id if msg.reply_to_message is not None else None,
        raw=msg.model_dump(mode="json", exclude_none=True),
        media=media_of(msg),
        voice=voice_of(msg),
    )
