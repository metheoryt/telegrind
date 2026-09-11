"""The two model calls that bracket the arithmetic.

The model says *what* to count and later reads the result out loud. It
never adds anything up: between these two calls sits Postgres.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import date

from telegrind import llm, query
from telegrind.config import ChatConfig
from telegrind.query import Spec


async def spec_for(
    question: str,
    vocabulary: str,
    cfg: ChatConfig,
    today: date,
    *,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> Spec:
    """Question → spec. Raises Unanswerable rather than guessing."""
    system = llm.QUERY_SYSTEM_TEMPLATE.format(today=today.isoformat())
    user = f"# Словарь этого чата\n{vocabulary}\n\n# Вопрос\n{question}"
    payload = await call(system, user, llm.QUERY_TOOL)
    return Spec.parse(payload, cfg)


async def render(
    question: str,
    spec: Spec,
    result: query.Answer,
    cfg: ChatConfig,
    *,
    say: Callable[..., Awaitable[str]] = llm.say,
) -> str:
    """Numbers → prose. Short-circuits on an empty result.

    An empty result needs no model: there is nothing to phrase, and a
    model asked to phrase nothing invents a reason.
    """
    if not result.rows or all(row.value is None and row.n == 0 for row in result.rows):
        return "По этому вопросу записей нет."

    payload = {
        "question": question,
        "aggregate": spec.aggregate,
        "field": spec.field,
        "currency": cfg.currency,
        "rows": [
            {"group": row.group, "value": row.value, "n": row.n} for row in result.rows
        ],
        "skipped": result.skipped,
    }
    return await say(
        llm.ANSWER_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str)
    )
