from datetime import datetime, timedelta, timezone

from telegrind.llm import (
    PROMPT_VERSION,
    build_schema,
    build_system_prompt,
    build_user_message,
)
from telegrind.registry import Category, Column, Registry
from telegrind.sheets import Config

CFG = Config(dt_offset=6, currency="KZT")

EXPENSE = Category(
    name="expense",
    worksheet="Expenses",
    when_to_use="потраченная сумма",
    columns=(
        Column("Сумма", "money"),
        Column("Валюта", "currency"),
        Column("Дата", "date"),
        Column("Комментарий", "text"),
    ),
)
TELEMETRY = Category(
    name="telemetry",
    worksheet="Telemetry",
    when_to_use="измерение о себе",
    columns=(
        Column("Метрика", "text"),
        Column("Значение", "number"),
        Column("Дата", "date"),
    ),
)
LOAN = Category(
    name="loan",
    worksheet="Loans",
    when_to_use="долг",
    columns=(Column("Сумма", "money"), Column("Срок", "due")),
)
REG = Registry((EXPENSE, TELEMETRY, LOAN))
REG_SCHEMA = build_schema(REG)


def branches(schema: dict) -> list[dict]:
    return schema["properties"]["facts"]["items"]["anyOf"]


def test_schema_wraps_an_array_of_anyof_branches() -> None:
    schema = build_schema(REG)
    assert schema["type"] == "object"
    assert schema["required"] == ["facts"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["facts"]["type"] == "array"
    assert len(branches(schema)) == 3


def test_one_branch_per_category_discriminated_by_a_const() -> None:
    consts = [b["properties"]["category"]["const"] for b in branches(REG_SCHEMA)]
    assert consts == ["expense", "telemetry", "loan"]


def test_every_branch_closes_additional_properties() -> None:
    assert all(b["additionalProperties"] is False for b in branches(REG_SCHEMA))


def test_every_column_is_required_alongside_the_discriminator() -> None:
    expense = branches(REG_SCHEMA)[0]
    assert expense["required"] == ["category", "Сумма", "Валюта", "Дата", "Комментарий"]


def test_money_and_number_become_json_numbers() -> None:
    expense, telemetry = branches(REG_SCHEMA)[0], branches(REG_SCHEMA)[1]
    assert expense["properties"]["Сумма"]["type"] == "number"
    assert telemetry["properties"]["Значение"]["type"] == "number"


def test_text_becomes_a_json_string() -> None:
    assert branches(REG_SCHEMA)[0]["properties"]["Комментарий"] == {"type": "string"}


def test_date_and_due_both_declare_the_date_time_format() -> None:
    date_prop = branches(REG_SCHEMA)[0]["properties"]["Дата"]
    due_prop = branches(REG_SCHEMA)[2]["properties"]["Срок"]
    assert date_prop["type"] == due_prop["type"] == "string"
    assert date_prop["format"] == due_prop["format"] == "date-time"


def test_date_and_due_differ_only_in_their_description() -> None:
    date_prop = branches(REG_SCHEMA)[0]["properties"]["Дата"]
    due_prop = branches(REG_SCHEMA)[2]["properties"]["Срок"]
    assert "past" in date_prop["description"]
    assert "future" in due_prop["description"]


def test_currency_describes_iso_4217() -> None:
    prop = branches(REG_SCHEMA)[0]["properties"]["Валюта"]
    assert prop["type"] == "string"
    assert "4217" in prop["description"]


def test_the_key_column_is_never_in_the_schema() -> None:
    assert all("#" not in b["properties"] for b in branches(REG_SCHEMA))


def test_the_prompt_names_every_category_and_its_when_to_use() -> None:
    prompt = build_system_prompt(REG, CFG)
    for cat in REG.categories:
        assert cat.name in prompt
        assert cat.when_to_use in prompt


def test_the_prompt_states_the_loan_sign_convention() -> None:
    prompt = build_system_prompt(REG, CFG)
    assert "-100" in prompt
    assert "+100" in prompt


def test_the_prompt_names_the_default_currency() -> None:
    assert "KZT" in build_system_prompt(REG, CFG)


def test_the_prompt_carries_no_timestamp() -> None:
    """A `now` in the cached prefix invalidates the cache on every message."""
    prompt = build_system_prompt(REG, CFG)
    assert "2026" not in prompt


def test_the_user_message_carries_the_timestamp_and_the_content() -> None:
    now = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))
    msg = build_user_message("4500 такси", now)
    assert "2026-09-09T21:40:00+06:00" in msg
    assert "4500 такси" in msg


def test_prompt_version_is_set() -> None:
    assert PROMPT_VERSION
