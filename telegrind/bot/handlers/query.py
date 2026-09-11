"""/q — the override, kept because it costs nothing.

The classifier takes questions now, so asking no longer requires a command.
/q stays because it is useful twice: when the user wants to be sure they are
asking, and when the classifier got it wrong. It sets the verdict rather
than bypassing it, which is why there is no second answering path here —
see bot/answering.py for that half.
"""

from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.answering import REFUSAL, answer_for, question_of
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI, acknowledge
from telegrind.bot.outbound import say
from telegrind.bot.router import router
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
    """Store the question, catch the log up, then answer it."""
    async with session.begin():
        row, _ = await store.upsert_message(
            session, chat, message, extractable=False, verdict=VERDICT_QUESTION
        )
        row.receipt_emoji = RECEIPT_EMOJI
    await acknowledge(bot, message.chat.id, message.message_id, RECEIPT_EMOJI)

    # Said in a transaction of its own, and outside the answering one: the
    # first /q after a quiet week pays for the week, and holding a write
    # transaction open across two model calls to announce that is the wrong
    # shape even at one user. A bare read here would autobegin and make the
    # `session.begin()` below raise «a transaction is already begun».
    async with session.begin():
        pending = len(await store.unextracted_tail(session, chat.id))
    if pending:
        await say(bot, session, chat, f"Разбираю {pending} сообщений…")

    async with session.begin():
        text = await answer_for(question_of(message.text), chat, config, session)
    await say(bot, session, chat, text or REFUSAL, reply_to=message.message_id)
