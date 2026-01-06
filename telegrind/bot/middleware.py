from aiogram.types import Chat as TGChat
from aiogram.types import Message, Update
from gspread_asyncio import AsyncioGspreadClient, AsyncioGspreadClientManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.models import Chat

from .dispatcher import dp


@dp.update.middleware()
async def populate_chat_data(handler, event: Update, data: dict):
    if not isinstance(event.event, Message):
        return
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

            if not chat.sheet_url:
                bot = data["bot"]
                tgchat: TGChat = await bot.get_chat(msg.chat.id)
                chat.sheet_url = tgchat.pinned_message.text
                session.add(chat)

        agcm: AsyncioGspreadClientManager = data["agcm"]
        agc: AsyncioGspreadClient = await agcm.authorize()

        data["agc"] = agc
        data["chat"] = chat
        data["session"] = session

        return await handler(event, data)
