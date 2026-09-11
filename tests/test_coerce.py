from datetime import UTC, datetime

from telegrind.coerce import to_instant, to_json_value
from telegrind.config import ChatConfig

CFG = ChatConfig(tz_offset=6, currency="KZT")
#: 2026-09-09 21:40 in Almaty — the message's own timestamp.
FALLBACK = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)


def test_an_integer_string_becomes_a_json_number() -> None:
    assert to_json_value("4500") == 4500
    assert isinstance(to_json_value("4500"), int)


def test_a_decimal_string_becomes_a_float() -> None:
    assert to_json_value("12.5") == 12.5


def test_a_number_passes_through() -> None:
    assert to_json_value(4500) == 4500
    assert to_json_value(12.5) == 12.5


def test_spaced_thousands_do_not_become_a_number() -> None:
    """`5 000` is ambiguous — two numbers or one? Keep the user's text."""
    assert to_json_value("5 000") == "5 000"


def test_prose_stays_prose() -> None:
    assert to_json_value("около 500") == "около 500"
    assert to_json_value("много") == "много"


def test_none_stays_none() -> None:
    assert to_json_value(None) is None


def test_a_bool_is_not_coerced_to_a_number() -> None:
    assert to_json_value(True) is True


def test_a_negative_amount_survives() -> None:
    """The loan sign convention depends on it."""
    assert to_json_value("-100") == -100


def test_an_iso_string_becomes_that_instant() -> None:
    assert to_instant("2026-08-01T12:00:00+06:00", CFG, FALLBACK).month == 8


def test_a_relative_russian_date_resolves_against_the_message() -> None:
    assert to_instant("вчера", CFG, FALLBACK).date().day == 8


def test_an_unparseable_date_falls_back_to_the_message_timestamp() -> None:
    assert to_instant("когда-нибудь", CFG, FALLBACK) == FALLBACK


def test_an_absent_date_falls_back_to_the_message_timestamp() -> None:
    assert to_instant(None, CFG, FALLBACK) == FALLBACK
    assert to_instant("", CFG, FALLBACK) == FALLBACK


def test_a_naive_parse_gets_the_chat_offset() -> None:
    assert to_instant("", CFG, FALLBACK).tzinfo is not None


def test_prefer_future_is_what_separates_a_due_date_from_an_event() -> None:
    """The same «во вторник» resolves forward for a deadline, back for an event."""
    past = to_instant("во вторник", CFG, FALLBACK)
    future = to_instant("во вторник", CFG, FALLBACK, prefer_future=True)
    assert past < FALLBACK < future


def test_the_reference_clock_is_the_message_not_today() -> None:
    """Deferred extraction runs later than the message; «вчера» must not drift."""
    old = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    assert to_instant("вчера", CFG, old).date().day == 14


def test_late_at_night_today_is_the_chats_today_not_utcs() -> None:
    """02:00 in Almaty is still the previous day in UTC.

    dateparser resolves «сегодня» against RELATIVE_BASE, so that base has
    to be the chat's wall clock or every fact written after midnight local
    time lands on the wrong day.
    """
    late = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)  # 2026-09-10 02:00 +06:00
    assert to_instant("сегодня", CFG, late).date().day == 10
    assert to_instant("вчера", CFG, late).date().day == 9
