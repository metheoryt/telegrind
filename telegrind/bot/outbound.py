"""The only way the bot speaks.

Every outgoing message is stored, because a reply to something the bot said
has to resolve to a row: without one, `extract._line` calls it «ответ на
сообщение вне окна», and the second hop of a session lookup has nothing to
read. `verdict=system` keeps it out of the extraction tail — it is the bot's
own text, and a taxonomy coined from it would be the bot reading itself.
"""

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.types import Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.models import VERDICT_SYSTEM, Chat

#: aiogram's «use whatever DefaultBotProperties says» sentinel, which is
#: what `send_message` itself defaults `parse_mode` to. A module constant
#: rather than a `Default("parse_mode")` written in the signature, because
#: a call in an argument default is a bugbear error here (B008).
#:
#: Passing `parse_mode=None` instead is what overrides the bot-wide HTML:
#: verified against the installed aiogram 3.27 tree on 2026-09-12,
#: `BaseSession.prepare_value` returns None before it ever reaches the
#: Default branch, and `build_form_data` drops falsy values from the
#: request — so the field is simply absent and Telegram parses nothing.
BOT_DEFAULT = Default("parse_mode")


async def say(
    bot: Bot,
    session: AsyncSession,
    chat: Chat,
    text: str,
    *,
    reply_to: int | None = None,
    parse_mode: str | Default | None = BOT_DEFAULT,
) -> Message:
    """Send, then store what was sent. Opens its own transaction.

    `reply_to_message_id` is deprecated at aiogram 3.27; ReplyParameters is
    the field the Bot API 9.6 schema actually carries.

    `parse_mode` is a pass-through, defaulting to the bot's own default so
    that every caller of hand-written prose is unaffected. It exists for
    model-authored text, which the meta layer sends with `None`: HTML
    parsing turns a stray `<` into «can't parse entities» and loses the
    whole message.
    """
    sent = await bot.send_message(
        chat.chat_id,
        text,
        reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None,
        parse_mode=parse_mode,
    )
    async with session.begin():
        await store.upsert_message(
            session, chat, sent, extractable=False, verdict=VERDICT_SYSTEM
        )
    return sent
