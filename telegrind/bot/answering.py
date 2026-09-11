"""Question → numbers → prose. No Telegram in here, so it tests.

Split out of `handlers/query.py` because it registers nothing. Importing a
handler module is what registers its handlers, and `bot/routing.py` needs
this half — importing it from `query.py` would drag the `/q` registration
into a cycle with routing, which `query.py` also needs.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import answer as answers
from telegrind import extract, query, taxonomy
from telegrind.config import ChatConfig
from telegrind.models import Chat

log = logging.getLogger(__name__)

#: What the bot says when the question does not fit the closed set of
#: aggregates and there is nobody to hand it to.
REFUSAL = "Не понял вопрос, переформулируй."
EMPTY_QUESTION = "Спроси что-нибудь после /q."


def question_of(text: str | None) -> str:
    """The question, with a leading /q stripped if the user used one.

    It now sees plain text too: a natural-language question never had a
    command in front of it. An @mention suffix is still tolerated.
    """
    body = (text or "").strip()
    if not body.startswith("/q"):
        return body
    _, _, rest = body.partition(" ")
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
) -> str | None:
    """The whole answer as one string, or None if the question does not fit.

    None rather than the refusal text: `answer.spec_for` already refuses a
    question it cannot express rather than approximating it, and that
    refusal is a hand-off, not a dead end — but only a caller that can see
    it is a refusal can hand it anywhere.
    """
    if not question:
        return EMPTY_QUESTION

    report = await passes(session, chat, config)

    words = await vocabulary(session, chat.id)
    today = datetime.now(tz=config.tz).date()
    try:
        spec = await spec_for(question, words, config, today)
    except query.Unanswerable as exc:
        log.info("unanswerable question in chat %s: %s", chat.chat_id, exc)
        return None

    result = await query_run(session, chat.id, spec)
    text = await render(question, spec, result, config)

    if report.failed:
        text += f"\n\n({report.failed} сообщений не удалось разобрать.)"
    if report.complaints:
        text += f"\n({report.complaints} фактов не удалось привязать.)"
    return text
