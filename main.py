import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegrind.bot.setup import setup_dispatcher


async def main() -> None:
    dp = setup_dispatcher()
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    token = os.environ["BOT_TOKEN"]
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot, async_session=async_session)

    await engine.dispose()


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    )
    asyncio.run(main())
