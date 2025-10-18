import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread_asyncio import AsyncioGspreadClientManager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from telegrind.bot.setup import setup_dispatcher
from telegrind.models import Model


def get_creds():
    # To obtain a service account JSON file, follow these steps:
    # https://gspread.readthedocs.io/en/latest/oauth2.html#for-bots-using-service-account
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    )
    scoped = creds.with_scopes(
        [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
    )
    return scoped


async def main() -> None:
    dp = setup_dispatcher()
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all, checkfirst=True)

    async_session = async_sessionmaker(engine, expire_on_commit=False)

    token = os.environ["BOT_TOKEN"]
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    # And the run events dispatching
    await dp.start_polling(
        bot, async_session=async_session, agcm=AsyncioGspreadClientManager(get_creds)
    )

    # teardown
    await engine.dispose()


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    )
    asyncio.run(main())
