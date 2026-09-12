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
    # expire_on_commit=False is load-bearing, not tidiness. `record` commits
    # the message row and then hands it to `bot/routing.py`, which reads
    # `row.verdict` — and `chat.id` off a Chat the middleware committed
    # earlier. Under the default True, commit expires those attributes and
    # the next read has to refresh them, which is IO on an object nobody is
    # in a transaction for. The likeliest traceback is
    # `sqlalchemy.exc.MissingGreenlet` at the attribute access — implicit IO
    # with no greenlet on the stack — rather than anything mentioning
    # transactions; if it does get its SELECT away it autobegins one nothing
    # closes, and the question arm's next `session.begin()` raises «a
    # transaction is already begun». Either way the fix is this argument.
    # No test can catch its removal: the suite's hand-written fake sessions
    # have no transaction state at all, so it goes green and fails on the box.
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
