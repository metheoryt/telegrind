import asyncio
import logging
import os

from aiogram import Bot
from aiogram.utils.chat_action import ChatActionMiddleware
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread_asyncio import (
    AsyncioGspreadClientManager
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from telegrind.handlers import router, dp
from telegrind.models import Model


def get_creds():
    # To obtain a service account JSON file, follow these steps:
    # https://gspread.readthedocs.io/en/latest/oauth2.html#for-bots-using-service-account
    creds = Credentials.from_service_account_file(os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE'))
    scoped = creds.with_scopes([
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return scoped


async def main() -> None:
    # ... and all other routers should be attached to Dispatcher
    router.message.middleware(ChatActionMiddleware())
    dp.include_router(router)
    engine = create_async_engine(os.getenv('DATABASE_URL'), echo=True)
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all, checkfirst=True)

    async_session = async_sessionmaker(engine, expire_on_commit=False)

    token = os.getenv('BOT_TOKEN')
    bot = Bot(token, parse_mode="HTML")
    # And the run events dispatching
    await dp.start_polling(
        bot,
        async_session=async_session,
        agcm=AsyncioGspreadClientManager(get_creds)
    )

    # teardown
    await engine.dispose()


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
