"""aiogram messages as a handler sees them, and (from Task 8) a fake Bot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram.types import (
    Chat,
    Document,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
    Sticker,
    User,
    VideoNote,
    Voice,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def user(uid: int = 7, name: str = "Max") -> User:
    return User(id=uid, is_bot=False, first_name=name)


def bot_user() -> User:
    return User(id=999, is_bot=True, first_name="telegrind", username="telegrind_bot")


def user_message(
    uid: int = 7,
    message_id: int = 10,
    text: str | None = None,
    *,
    caption: str | None = None,
    photo: bool = False,
    document: tuple[str, str, int] | None = None,
    voice: int | None = None,
    video_note: bool = False,
    sticker: bool = False,
    reply_to: Message | None = None,
    forward: tuple[str, str] | None = None,
    chat_type: str = "private",
    date: datetime = NOW,
    edit_date: int | None = None,
    from_bot: bool = False,
) -> Message:
    chat_id = uid if chat_type == "private" else -1001
    origin = None
    if forward:
        kind, name = forward
        odate = date - timedelta(days=7)
        origin = {
            "user": lambda: MessageOriginUser(
                type="user",
                date=odate,
                sender_user=User(
                    id=5,
                    is_bot=False,
                    first_name=name.split()[0],
                    last_name=" ".join(name.split()[1:]) or None,
                ),
            ),
            "hidden": lambda: MessageOriginHiddenUser(type="hidden_user", date=odate, sender_user_name=name),
            "chat": lambda: MessageOriginChat(
                type="chat", date=odate, sender_chat=Chat(id=-5, type="group", title=name)
            ),
            "channel": lambda: MessageOriginChannel(
                type="channel", date=odate, chat=Chat(id=-6, type="channel", title=name), message_id=1
            ),
        }[kind]()
    return Message(
        message_id=message_id,
        date=date,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=bot_user() if from_bot else user(uid),
        text=text,
        caption=caption,
        photo=[
            PhotoSize(file_id="p-small", file_unique_id="u1", width=90, height=90, file_size=1000),
            PhotoSize(file_id="p-big", file_unique_id="u2", width=1280, height=1280, file_size=250_000),
        ]
        if photo
        else None,
        document=Document(
            file_id=document[0],
            file_unique_id="d1",
            file_name=document[1],
            mime_type="application/pdf",
            file_size=document[2],
        )
        if document
        else None,
        voice=Voice(file_id="v1", file_unique_id="vu1", duration=voice, file_size=voice * 2000)
        if voice is not None
        else None,
        video_note=VideoNote(file_id="vn1", file_unique_id="vnu1", length=240, duration=12) if video_note else None,
        sticker=Sticker(
            file_id="s1", file_unique_id="su1", type="regular", width=512, height=512, is_animated=False, is_video=False
        )
        if sticker
        else None,
        reply_to_message=reply_to,
        forward_origin=origin,
        edit_date=edit_date,
    )


def bot_message(uid: int = 7, message_id: int = 500, text: str = "ok", reply_to: Message | None = None) -> Message:
    return Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=uid, type="private"),
        from_user=bot_user(),
        text=text,
        reply_to_message=reply_to,
    )
