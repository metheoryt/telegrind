"""Anthropic plumbing and the extraction rules corpus.

Phase 1 makes no LLM call. What is kept here is what Phase 2 needs and
cannot re-derive: the client, the model names, and the rules prose, which
is accumulated judgement about real messages rather than code.
"""

import logging
import os
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import ToolParam

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-09-11.1"

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048

#: Kept verbatim from the registry-era system prompt. Phase 2 composes this
#: with the observed taxonomy; the judgement in it does not depend on how
#: the categories are declared, so it outlives the registry.
EXTRACTION_RULES = """\
- One message may hold several facts. Return one array element each.
- Return an empty array only for a message that states no fact at all.
- Anything you cannot confidently place goes to the `facts` category,
  with the message text kept verbatim. Never drop a fact.
- When a message opens with an amount of money and no other category
  fits it, it is an `expense`, and the rest of the message is the
  comment: `4500 такси`, `444 куколд`, `300 фигня`, and `444` on its
  own with an empty comment. Do not fall back to `facts` because the
  comment names nothing you recognise as buyable — what it was spent
  on is not your judgement to make.
- Loan amounts carry a sign convention: a loan given out is negative
  (-100), a repayment received is positive (+100). A bare amount with
  no direction stated means a loan given out, so -100.
- Do not invent fields. Do not invent values. An unstated text field
  is an empty string.
- Keep the user's own wording in text fields; do not translate it.
- A date the message mentions *about* the thing is not the date of the
  fact. `билеты на 15 октября` was bought now and the flight is on the
  15th; `оплатил квартиру за октябрь` was paid now. Date the fact to
  when it happened, put the mentioned date in a `due` field if the
  category has one, and otherwise keep it in the text field.
"""


def client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def current_model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


class LLMError(RuntimeError):
    """The model did not answer in the shape we asked for."""


EXTRACT_SYSTEM = f"""\
Ты извлекаешь факты из личного дневника в Telegram. Тебе дают окно
сообщений подряд — читай их вместе, соседние сообщения часто продолжают
друг друга.

{EXTRACTION_RULES}
- Поле `when` — это фраза о времени ровно так, как она написана в
  сообщении («вчера вечером», «в понедельник», «15 октября»). Не считай
  даты сам: у каждого сообщения свой час, и арифметику делает код.
  Если сообщение не называет времени — пустая строка.
- `message` — номер сообщения из блока «Сообщения для разбора».
  Факт, собранный из нескольких сообщений, принадлежит ПОСЛЕДНЕМУ из них:
  там он стал полным.
- Из блока «Контекст» извлекать не надо. Он нужен только чтобы понять,
  о чём речь.
"""

EXTRACT_TOOL: ToolParam = {
    "name": "record_facts",
    "description": "Записать факты, извлечённые из окна сообщений.",
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "integer"},
                        "kind": {"type": "string"},
                        "when": {"type": "string"},
                        "fields": {"type": "object"},
                    },
                    "required": ["message", "kind", "fields"],
                },
            }
        },
        "required": ["facts"],
    },
}


async def use_tool(
    system: str, user: str, tool: ToolParam, *, model: str | None = None
) -> dict[str, Any]:
    """One forced tool call. Returns the tool input, already a dict.

    Forced rather than suggested: the caller needs a structure, and an
    unforced call is free to answer in prose, which is a parse error
    dressed up as a success.
    """
    response = await client().messages.create(
        model=model or current_model(),
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise LLMError(f"{tool['name']} was not called")


async def say(system: str, user: str, *, model: str | None = None) -> str:
    """A plain prose answer."""
    response = await client().messages.create(
        model=model or current_model(),
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


QUERY_SYSTEM_TEMPLATE = """\
Ты превращаешь вопрос о личном дневнике в структуру запроса. Ты НЕ
считаешь — считает база.

Сегодня {today}. Периоды задавай ISO-датами, `until` не включается:
«за август» это since=2026-08-01, until=2026-09-01.

Агрегаты: sum, count, avg, min, max, last, balance_by.
- sum — расходы и всё, что складывается.
- count — привычки и события: сколько раз.
- last / min / max — ряд измерений: последний вес, минимальный, максимальный.
- balance_by — сальдо по каждому контрагенту; group_by обязателен.
Если вопрос не ложится ни на один агрегат — верни aggregate «unknown».
Лучше честное «не понял», чем неправильное число.
"""

QUERY_TOOL: ToolParam = {
    "name": "build_query",
    "description": "Описать, что посчитать и по каким фактам.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kinds": {"type": "array", "items": {"type": "string"}},
            "aggregate": {"type": "string"},
            "field": {"type": "string"},
            "since": {"type": "string"},
            "until": {"type": "string"},
            "group_by": {"type": "string"},
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["field", "value"],
                },
            },
        },
        "required": ["aggregate"],
    },
}

ANSWER_SYSTEM = """\
Ты отвечаешь на вопрос по уже посчитанным числам. Числа даны — не меняй
их и не считай новых. Отвечай коротко, по-русски, одной-двумя фразами.
Если часть записей не попала в сумму, скажи об этом одной фразой.
"""
