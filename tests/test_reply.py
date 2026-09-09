from types import SimpleNamespace

from telegrind.bot.handlers.handlers import COMMAND_LIKE, format_records
from telegrind.models import Fact


def fact(worksheet: str, key: str, fields: dict[str, object]) -> Fact:
    return Fact(worksheet=worksheet, sheet_key=key, fields=fields, category="x", seq=1)


def test_one_record_names_the_worksheet_and_the_values() -> None:
    out = format_records(
        [
            fact(
                "Expenses",
                "4821_1",
                {
                    "Сумма": 4500.0,
                    "Валюта": "KZT",
                    "Дата": "09.09.26 21:40",
                    "Комментарий": "такси",
                },
            )
        ]
    )
    assert "Expenses" in out
    assert "4500" in out
    assert "такси" in out


def test_the_pointer_is_hidden_in_a_spoiler() -> None:
    out = format_records([fact("Expenses", "4821_1", {"Сумма": 1.0})])
    assert "<tg-spoiler>4821_1@Expenses</tg-spoiler>" in out


def test_empty_fields_are_dropped_from_the_line() -> None:
    out = format_records(
        [fact("Wishlist", "9_1", {"Желание": "велосипед", "Исполнено": ""})]
    )
    assert "велосипед" in out
    assert " ·  · " not in out


def test_several_records_are_counted() -> None:
    out = format_records(
        [
            fact("Expenses", "4821_1", {"Сумма": 1.0}),
            fact("Telemetry", "4821_2", {"Значение": 82.4}),
        ]
    )
    assert "2" in out.splitlines()[0]
    assert len(out.splitlines()) == 3


def test_a_single_record_is_not_counted() -> None:
    out = format_records([fact("Expenses", "4821_1", {"Сумма": 1.0})])
    assert len(out.splitlines()) == 2


def test_no_records_says_so() -> None:
    assert format_records([]).strip()


def test_a_mistyped_command_is_not_treated_as_a_fact() -> None:
    """/rebiuld must not be extracted into the Facts sheet."""
    assert COMMAND_LIKE.resolve(SimpleNamespace(text="/rebiuld"))
    assert COMMAND_LIKE.resolve(SimpleNamespace(text="/help"))


def test_an_ordinary_message_is_not_command_like() -> None:
    assert not COMMAND_LIKE.resolve(SimpleNamespace(text="4500 такси"))
    assert not COMMAND_LIKE.resolve(SimpleNamespace(text="вес 82.4"))
