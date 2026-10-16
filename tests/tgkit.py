"""aiogram messages as a handler sees them, and (from Task 8) a fake Bot."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Chat,
    Document,
    File,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    MessageReactionUpdated,
    PhotoSize,
    ReactionTypeEmoji,
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


def reaction(
    uid: int = 7, message_id: int = 10, emoji: str | None = "💔", chat_type: str = "private"
) -> MessageReactionUpdated:
    return MessageReactionUpdated(
        chat=Chat(id=uid if chat_type == "private" else -1001, type=chat_type),
        message_id=message_id,
        date=NOW,
        user=user(uid),
        old_reaction=[] if emoji else [ReactionTypeEmoji(type="emoji", emoji="💔")],
        new_reaction=[ReactionTypeEmoji(type="emoji", emoji=emoji)] if emoji else [],
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


class FakeBot:
    """Records every call as (method, kwargs). `fail` maps a method to how many of its calls raise."""

    id = 999

    def __init__(self, *, fail: Mapping[str, int] | None = None, hang: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = dict(fail or {})
        self.hang = set(hang)
        self.files: dict[str, bytes] = {}
        self._last_id = 500

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def reactions(self) -> list[tuple[int, str | None]]:
        """(message_id, emoji or None) for every set_message_reaction, in order."""
        return [
            (kw["message_id"], kw["reaction"][0].emoji if kw["reaction"] else None)
            for name, kw in self.calls
            if name == "set_message_reaction"
        ]

    def sends(self) -> list[tuple[str, dict]]:
        return [(name, kw) for name, kw in self.calls if name in ("send_rich_message", "send_message")]

    def texts(self) -> list[str]:
        return [kw.get("text") or "" for _, kw in self.sends()]

    async def _call(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, kwargs))
        if name in self.hang:
            await asyncio.Event().wait()
        if self.fail.get(name, 0) > 0:
            self.fail[name] -= 1
            raise TelegramBadRequest(method=None, message=f"Bad Request: {name} rejected")

    def _sent(self, kwargs: dict) -> Message:
        self._last_id += 1
        reply = kwargs.get("reply_parameters")
        return Message(
            message_id=self._last_id,
            date=NOW,
            chat=Chat(id=kwargs["chat_id"], type="private"),
            from_user=bot_user(),
            text=kwargs.get("text"),
            reply_to_message=Message(
                message_id=reply.message_id, date=NOW, chat=Chat(id=kwargs["chat_id"], type="private")
            )
            if reply
            else None,
        )

    async def set_message_reaction(self, **kwargs) -> bool:
        await self._call("set_message_reaction", kwargs)
        return True

    async def send_chat_action(self, **kwargs) -> bool:
        await self._call("send_chat_action", kwargs)
        return True

    async def send_rich_message_draft(self, **kwargs) -> bool:
        await self._call("send_rich_message_draft", kwargs)
        return True

    async def send_rich_message(self, **kwargs) -> Message:
        await self._call("send_rich_message", kwargs)
        return self._sent(kwargs)

    async def send_message(self, **kwargs) -> Message:
        await self._call("send_message", kwargs)
        return self._sent(kwargs)

    async def get_file(self, file_id: str) -> File:
        await self._call("get_file", {"file_id": file_id})
        return File(
            file_id=file_id,
            file_unique_id=f"u-{file_id}",
            file_path=f"files/{file_id}",
            file_size=len(self.files.get(file_id, b"\x00" * 10)),
        )

    async def download_file(
        self, file_path: str, destination: BytesIO | Path | None = None, **kwargs
    ) -> BytesIO | None:
        await self._call("download_file", {"file_path": file_path})
        data = self.files.get(file_path.removeprefix("files/"), b"\x00" * 10)
        if destination is None:
            return BytesIO(data)
        if isinstance(destination, Path | str):
            Path(destination).write_bytes(data)
            return None
        destination.write(data)
        return destination
