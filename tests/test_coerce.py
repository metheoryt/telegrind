from datetime import datetime, timedelta, timezone

from telegrind.coerce import (
    SHEET_DATETIME_FORMAT,
    coerce_currency,
    coerce_datetime,
    coerce_fields,
    coerce_number,
)
from telegrind.registry import Category, Column
from telegrind.sheets import Config

CFG = Config(dt_offset=6, currency="KZT")
FALLBACK = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))


def test_number_passes_a_float_through() -> None:
    assert coerce_number(82.4) == 82.4


def test_number_accepts_an_int() -> None:
    assert coerce_number(4500) == 4500.0


def test_number_normalizes_a_decimal_comma() -> None:
    assert coerce_number("82,4") == 82.4


def test_number_strips_a_currency_glyph_and_spaces() -> None:
    assert coerce_number("4 500 ₸") == 4500.0


def test_number_keeps_a_negative_sign() -> None:
    assert coerce_number("-100") == -100.0


def test_number_returns_empty_string_for_junk() -> None:
    assert coerce_number("не число") == ""


def test_number_returns_empty_string_for_none() -> None:
    assert coerce_number(None) == ""


def test_currency_upper_cases_a_valid_code() -> None:
    assert coerce_currency("usd", CFG) == "USD"


def test_currency_falls_back_to_config_when_absent() -> None:
    assert coerce_currency(None, CFG) == "KZT"


def test_currency_falls_back_to_config_when_invalid() -> None:
    assert coerce_currency("рублей", CFG) == "KZT"


def test_datetime_parses_iso_with_an_offset() -> None:
    out = coerce_datetime("2026-09-09T21:40:00+06:00", CFG, FALLBACK)
    assert out == FALLBACK.strftime(SHEET_DATETIME_FORMAT)


def test_datetime_localizes_an_iso_value_in_another_zone() -> None:
    out = coerce_datetime("2026-09-09T15:40:00+00:00", CFG, FALLBACK)
    assert out == "09.09.26 21:40"


def test_datetime_assumes_the_chat_zone_for_a_naive_iso_value() -> None:
    assert coerce_datetime("2026-09-09T21:40:00", CFG, FALLBACK) == "09.09.26 21:40"


def test_datetime_falls_back_to_dateparser() -> None:
    out = coerce_datetime("9 сентября 2026 21:40", CFG, FALLBACK)
    assert out == "09.09.26 21:40"


def test_datetime_falls_back_to_the_message_timestamp() -> None:
    assert coerce_datetime(None, CFG, FALLBACK) == "09.09.26 21:40"


def test_datetime_falls_back_to_the_message_timestamp_on_junk() -> None:
    assert coerce_datetime("когда-нибудь", CFG, FALLBACK) == "09.09.26 21:40"


def test_date_leans_past_and_due_leans_future() -> None:
    past = coerce_datetime("вторник", CFG, FALLBACK, prefer_future=False)
    future = coerce_datetime("вторник", CFG, FALLBACK, prefer_future=True)
    parsed_past = datetime.strptime(past, SHEET_DATETIME_FORMAT)
    parsed_future = datetime.strptime(future, SHEET_DATETIME_FORMAT)
    naive_fallback = FALLBACK.replace(tzinfo=None)
    assert parsed_past <= naive_fallback
    assert parsed_future >= naive_fallback


EXPENSE = Category(
    name="expense",
    worksheet="Expenses",
    when_to_use="?",
    columns=(
        Column("Сумма", "money"),
        Column("Валюта", "currency"),
        Column("Дата", "date"),
        Column("Комментарий", "text"),
    ),
)


def test_coerce_fields_keys_by_header_in_column_order() -> None:
    out = coerce_fields(
        EXPENSE,
        {"Сумма": "4500", "Валюта": "kzt", "Дата": None, "Комментарий": " такси "},
        CFG,
        FALLBACK,
    )
    assert list(out) == ["Сумма", "Валюта", "Дата", "Комментарий"]
    assert out["Сумма"] == 4500.0
    assert out["Валюта"] == "KZT"
    assert out["Дата"] == "09.09.26 21:40"
    assert out["Комментарий"] == "такси"


def test_coerce_fields_fills_a_missing_key() -> None:
    out = coerce_fields(EXPENSE, {"Сумма": 4500}, CFG, FALLBACK)
    assert out["Комментарий"] == ""
    assert out["Валюта"] == "KZT"
    assert out["Дата"] == "09.09.26 21:40"


def test_coerce_fields_ignores_a_field_the_registry_does_not_declare() -> None:
    out = coerce_fields(EXPENSE, {"Сумма": 1, "Придумано": "x"}, CFG, FALLBACK)
    assert "Придумано" not in out


def test_coerce_fields_never_writes_the_key_column() -> None:
    out = coerce_fields(EXPENSE, {"#": "9999_1", "Сумма": 1}, CFG, FALLBACK)
    assert "#" not in out
