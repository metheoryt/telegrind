"""/q — the override, kept because it costs nothing.

The classifier takes questions now, so asking no longer requires a command.
/q stays because it is useful twice: when the user wants to be sure they are
asking, and when the classifier got it wrong. It sets the verdict rather
than bypassing it, which is why there is no second answering path here —
see bot/routing.py for what happens next, and bot/answering.py for the
answer itself.
"""

from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.router import router
from telegrind.bot.routing import route
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_QUESTION, Chat


@router.message(Command("q"))
async def ask(
    message: Message,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Store the question, then route it exactly as a plain one is routed.

    No receipt: a question has no facts on it, so 💔 would promise a delete
    gesture that does nothing. `receipt_emoji` is cleared for the same
    reason — an edit reads it to advance the cycle.
    """
    # Imported here, not at module scope: `handlers/__init__` imports this
    # module first so /q wins the registration race, and pulling `handlers`
    # in from the top of this file would reverse that order.
    from telegrind.bot.handlers import handlers

    async with session.begin():
        row, _ = await store.upsert_message(
            session, chat, message, extractable=False, verdict=VERDICT_QUESTION
        )
        row.receipt_emoji = None

    await route(message, row, chat, config, session, bot, hand_over=handlers.HAND_OVER)
