import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message, Update
from gspread.exceptions import APIError, NoValidUrlKeyFound
from gspread_asyncio import AsyncioGspreadClient, AsyncioGspreadClientManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.models import Chat
from telegrind.registry import load_registry
from telegrind.sheets import load_config

from .dispatcher import dp

log = logging.getLogger(__name__)


@dp.update.middleware()
async def populate_chat_data(
    handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
    event: Update,
    data: dict[str, Any],
) -> Any:
    if not isinstance(event.event, Message):
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

        agcm: AsyncioGspreadClientManager = data["agcm"]
        agc: AsyncioGspreadClient = await agcm.authorize()

        # The workbook is per-update, so no handler opens it itself. It is
        # also optional, and its absence is the *normal* state: it is a
        # projection of the fact table, and it exists only once the user
        # links one with /link.
        ags = None
        if chat.sheet_url:
            try:
                ags = await agc.open_by_url(chat.sheet_url)
            except APIError, NoValidUrlKeyFound:
                log.warning("cannot open %s for chat %s", chat.sheet_url, chat.chat_id)
                ags = None

        # The registry and the config are therefore never None. With no
        # workbook they fall back to the seeded categories and to +06:00 /
        # KZT, which is what lets a fact be extracted and stored before any
        # spreadsheet exists. Both caches are keyed by chat, not by URL.
        cache_key = str(chat.id)
        config = await load_config(ags, cache_key)
        registry = await load_registry(ags, cache_key)

        data["agc"] = agc
        data["ags"] = ags
        data["chat"] = chat
        data["session"] = session
        data["registry"] = registry
        data["config"] = config

        return await handler(event, data)
