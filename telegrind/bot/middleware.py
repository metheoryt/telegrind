import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message, TelegramObject, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    the narrowing happens here. Anything that is not a message update is
    dropped: Task 6 of the Phase 1 plan widens this to let reactions
    through, and until then a reaction never reaches a handler.
    """
    if not isinstance(event, Update) or not isinstance(event.event, Message):
        return None
    msg: Message = event.event

    async_session: async_sessionmaker[AsyncSession] = data["async_session"]
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Chat).where(Chat.chat_id == msg.chat.id)
            )
            chat: Chat | None = result.scalar_one_or_none()
            if not chat:
                chat = Chat(chat_id=msg.chat.id)
                session.add(chat)

        data["chat"] = chat
        data["session"] = session

        return await handler(event, data)
