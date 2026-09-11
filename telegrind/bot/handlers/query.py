"""/q — the only reason extraction ever has to have happened.

Asking is the trigger. Deferred parsing and the dialogue product are one
mechanism, not two features that coexist, so there is no second trigger
here and no background flush.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import answer as answers
from telegrind import extract, query, store, taxonomy
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI, acknowledge
from telegrind.bot.router import router
from telegrind.config import ChatConfig
from telegrind.models import Chat

log = logging.getLogger(__name__)


def question_of(text: str | None) -> str:
    """Everything after /q, with an @mention suffix tolerated."""
    if not text:
        return ""
    _, _, rest = text.partition(" ")
    return rest.strip()


async def _vocabulary(session: AsyncSession, chat_pk: int) -> str:
    return taxonomy.render(await taxonomy.observed(session, chat_pk))


async def answer_for(
    question: str,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    *,
    passes: Callable[..., Awaitable[extract.Report]] = extract.run,
    spec_for: Callable[..., Awaitable[query.Spec]] = answers.spec_for,
    query_run: Callable[..., Awaitable[query.Answer]] = query.run,
    render: Callable[..., Awaitable[str]] = answers.render,
    vocabulary: Callable[..., Awaitable[str]] = _vocabulary,
) -> str:
    """The whole answer as one string. No Telegram in here, so it tests."""
    if not question:
        return "Спроси что-нибудь после /q."

    report = await passes(session, chat, config)

    words = await vocabulary(session, chat.id)
    today = datetime.now(tz=config.tz).date()
    try:
        spec = await spec_for(question, words, config, today)
    except query.Unanswerable as exc:
        log.info("unanswerable question in chat %s: %s", chat.chat_id, exc)
        return "Не понял вопрос, переформулируй."

    result = await query_run(session, chat.id, spec)
    text = await render(question, spec, result, config)

    if report.failed:
        text += f"\n\n({report.failed} сообщений не удалось разобрать.)"
    if report.complaints:
        text += f"\n({report.complaints} фактов не удалось привязать.)"
    return text


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
        row, _ = await store.upsert_message(session, chat, message, extractable=False)
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
        await bot.send_message(message.chat.id, f"Разбираю {pending} сообщений…")

    async with session.begin():
        text = await answer_for(question_of(message.text), chat, config, session)
    await bot.send_message(message.chat.id, text)
