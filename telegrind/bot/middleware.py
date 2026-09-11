import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import (
    Message,
    MessageReactionUpdated,
    TelegramObject,
    Update,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.config import ChatConfig
from telegrind.models import Chat

from .dispatcher import dp

log = logging.getLogger(__name__)


# ty: ignore[invalid-argument-type, missing-argument] — aiogram overloads
# `middleware()` as both the bare decorator and the direct registration,
# and ty resolves the zero-argument call against the wrong arm.
@dp.update.middleware()
async def populate_chat_data(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Resolve the chat row and hand the handler an open session.

    The signature is aiogram's own — `TelegramObject`, not `Update` — and
    the narrowing happens here. A reaction has to be named explicitly:
    this used to return None for everything that was not a Message, which
    silently dropped every reaction update before any handler saw it.
    """
    if not isinstance(event, Update):
        return None
    inner = event.event
    if isinstance(inner, Message | MessageReactionUpdated):
        chat_id = inner.chat.id
    else:
        return None

    async_session: async_sessionmaker[AsyncSession] = data["async_session"]
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
            chat: Chat | None = result.scalar_one_or_none()
            if not chat:
                chat = Chat(chat_id=chat_id)
                session.add(chat)

        data["chat"] = chat
        data["config"] = ChatConfig.of(chat)
        data["session"] = session

        return await handler(event, data)
