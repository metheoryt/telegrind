"""Extraction: registry -> JSON Schema -> one Anthropic structured-outputs call.

The schema is data, built per registry at runtime. That is why marvin is
gone: `marvin.extract_async(target=Expense)` needs a compile-time type, and
a category is a spreadsheet row.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from anthropic import AsyncAnthropic

from telegrind.registry import Category, Registry
from telegrind.sheets import Config

log = logging.getLogger(__name__)

#: Bump whenever the prompt or the schema shape changes. Every fact records
#: the version that produced it, which is what makes `/reparse --stale`
#: answerable in Phase 2.
PROMPT_VERSION = "2026-09-10.1"

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048

_DATE_DESCRIPTION = (
    "ISO 8601 date-time with a UTC offset. When the fact itself happened: when "
    "the money moved, when the measurement was taken, when the thing was done. "
    "NOT a date the message mentions *about* its subject. If the message gives "
    "no date for the fact itself, use the current time from the user message. "
    "An ambiguous reference resolves to the nearest matching moment in the past."
)
_DUE_DESCRIPTION = (
    "ISO 8601 date-time with a UTC offset. A date the fact points at rather "
    "than happened on: a deadline, a due date, the date something is booked or "
    "planned for. An ambiguous reference resolves to the nearest matching "
    "moment in the future."
)
_CURRENCY_DESCRIPTION = (
    "Three-letter uppercase ISO 4217 code, e.g. KZT, USD, EUR, THB. Translate "
    "names and symbols: тенге -> KZT, рублей -> RUB, бат -> THB, $ -> USD."
)


@dataclass(frozen=True, slots=True)
class RawFact:
    """One fact as the model returned it, before coercion."""

    category: str
    fields: dict[str, object]


def _property_schema(column_type: str) -> dict[str, object]:
    match column_type:
        case "number" | "money":
            return {"type": "number"}
        case "currency":
            return {"type": "string", "description": _CURRENCY_DESCRIPTION}
        case "date":
            return {
                "type": "string",
                "format": "date-time",
                "description": _DATE_DESCRIPTION,
            }
        case "due":
            return {
                "type": "string",
                "format": "date-time",
                "description": _DUE_DESCRIPTION,
            }
        case _:
            return {"type": "string"}


def _branch(cat: Category) -> dict[str, object]:
    properties: dict[str, object] = {"category": {"type": "string", "const": cat.name}}
    for column in cat.columns:
        properties[column.header] = _property_schema(column.type)
    return {
        "type": "object",
        "properties": properties,
        "required": ["category", *(c.header for c in cat.columns)],
        "additionalProperties": False,
    }


def build_schema(registry: Registry) -> dict[str, object]:
    """`{"facts": [ anyOf: one branch per category ]}`.

    Probe 1 (2026-09-09, claude-haiku-4-5, anthropic 0.97.0) confirmed that
    `anyOf` inside an array's `items` with a `const` discriminator is
    accepted, and that Cyrillic property names work verbatim.
    """
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {"anyOf": [_branch(c) for c in registry.categories]},
            }
        },
        "required": ["facts"],
        "additionalProperties": False,
    }


def build_system_prompt(registry: Registry, cfg: Config) -> str:
    """The cached prefix. It must never contain a timestamp."""
    lines = [
        "You extract structured facts from a personal life-log message.",
        "",
        "Categories:",
    ]
    for cat in registry.categories:
        headers = ", ".join(f"{c.header} ({c.type})" for c in cat.columns)
        lines.append(f"- {cat.name} — {cat.when_to_use}. Fields: {headers}.")
    lines += [
        "",
        "Rules:",
        "- One message may hold several facts. Return one array element each.",
        "- Return an empty array only for a message that states no fact at all.",
        "- Anything you cannot confidently place goes to the `facts` category,",
        "  with the message text kept verbatim. Never drop a fact.",
        "- When a message opens with an amount of money and no other category",
        "  fits it, it is an `expense`, and the rest of the message is the",
        "  comment: `4500 такси`, `444 куколд`, `300 фигня`, and `444` on its",
        "  own with an empty comment. Do not fall back to `facts` because the",
        "  comment names nothing you recognise as buyable — what it was spent",
        "  on is not your judgement to make.",
        f"- When no currency is given, use {str(cfg.currency).upper()}.",
        "- Loan amounts carry a sign convention: a loan given out is negative",
        "  (-100), a repayment received is positive (+100). A bare amount with",
        "  no direction stated means a loan given out, so -100.",
        "- Do not invent fields. Do not invent values. An unstated text field",
        "  is an empty string.",
        "- Keep the user's own wording in text fields; do not translate it.",
        "- A date the message mentions *about* the thing is not the date of the",
        "  fact. `билеты на 15 октября` was bought now and the flight is on the",
        "  15th; `оплатил квартиру за октябрь` was paid now. Date the fact to",
        "  when it happened, put the mentioned date in a `due` field if the",
        "  category has one, and otherwise keep it in the text field.",
    ]
    return "\n".join(lines)


def build_user_message(content: str, now: datetime) -> str:
    """The uncached suffix. The timestamp lives here, deliberately.

    Prompt caching is a prefix match, so interpolating `now` into the system
    prompt — as today's EXTRACT_INSTRUCTION does — would invalidate the cache
    on every single message.
    """
    return f"Current time: {now.isoformat()}\n\nMessage:\n{content}"


def _client() -> AsyncAnthropic:
    return AsyncAnthropic()


def current_model() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODEL


async def extract(
    content: str,
    registry: Registry,
    cfg: Config,
    now: datetime,
    model: str | None = None,
) -> tuple[list[RawFact], str]:
    """One call, zero or more facts. Returns the facts and the model used."""
    model = model or current_model()
    resp = await _client().messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": build_system_prompt(registry, cfg),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": build_user_message(content, now)}],
        output_config={
            "format": {"type": "json_schema", "schema": build_schema(registry)}
        },
    )

    if resp.stop_reason == "refusal":
        log.warning("extraction refused: %s", resp.stop_details)
        return [], model
    if resp.stop_reason == "max_tokens":
        log.warning("extraction hit max_tokens; output is truncated JSON")
        return [], model

    try:
        payload = json.loads(resp.content[0].text)
    except IndexError, AttributeError, json.JSONDecodeError:
        log.exception("extraction returned unreadable output")
        return [], model

    facts: list[RawFact] = []
    for item in payload.get("facts", []):
        category = item.get("category")
        if registry.by_name(category) is None:
            log.warning("model returned unknown category %r, dropping", category)
            continue
        facts.append(
            RawFact(
                category=category,
                fields={k: v for k, v in item.items() if k != "category"},
            )
        )
    return facts, model
