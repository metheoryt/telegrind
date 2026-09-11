"""The only way the bot speaks.

Every outgoing message is stored, because a reply to something the bot said
has to resolve to a row: without one, `extract._line` calls it «ответ на
сообщение вне окна», and the second hop of a session lookup has nothing to
read. `verdict=system` keeps it out of the extraction tail — it is the bot's
own text, and a taxonomy coined from it would be the bot reading itself.
"""

from aiogram import Bot
from aiogram.types import Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.models import VERDICT_SYSTEM, Chat


async def say(
    bot: Bot,
    session: AsyncSession,
    chat: Chat,
    text: str,
    *,
    reply_to: int | None = None,
) -> Message:
    """Send, then store what was sent. Opens its own transaction.

    `reply_to_message_id` is deprecated at aiogram 3.27; ReplyParameters is
    the field the Bot API 9.6 schema actually carries.
    """
    sent = await bot.send_message(
        chat.chat_id,
        text,
        reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None,
    )
    async with session.begin():
        await store.upsert_message(
            session, chat, sent, extractable=False, verdict=VERDICT_SYSTEM
        )
    return sent
