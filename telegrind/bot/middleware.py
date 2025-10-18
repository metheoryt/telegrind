from gspread_asyncio import AsyncioGspreadClientManager, AsyncioGspreadClient

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from telegrind.models import Chat
from .dispatcher import dp


@dp.update.middleware()
async def populate_chat_data(handler, event, data):
    async_session: async_sessionmaker[AsyncSession] = data["async_session"]
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Chat).where(Chat.chat_id == event.event.chat.id)
            )
            chat = result.scalar_one_or_none()
            if not chat:
                chat = Chat(chat_id=event.event.chat.id)
                session.add(chat)

        agcm: AsyncioGspreadClientManager = data["agcm"]
        agc: AsyncioGspreadClient = await agcm.authorize()

        data["agc"] = agc
        data["chat"] = chat
        data["session"] = session

        return await handler(event, data)
