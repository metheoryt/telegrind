from datetime import UTC, datetime, timedelta, timezone

from telegrind.config import ChatConfig
from telegrind.models import Chat


def test_defaults_are_almaty_and_tenge() -> None:
    """A chat nobody configured gets +06:00 and KZT.

    The assertion is on the column, not on `Chat(chat_id=1).tz_offset`:
    SQLAlchemy applies a mapped default on INSERT, so an unflushed
    instance carries None and would make this test vacuous.
    """
    assert Chat.__table__.c.tz_offset.default.arg == 6
    assert Chat.__table__.c.currency.default.arg == "KZT"


def test_tz_is_a_fixed_offset() -> None:
    assert ChatConfig(tz_offset=6, currency="KZT").tz == timezone(timedelta(hours=6))


def test_localized_moves_an_utc_instant_into_the_chat_offset() -> None:
    cfg = ChatConfig(tz_offset=6, currency="KZT")
    local = cfg.localized(datetime(2026, 9, 9, 15, 40, tzinfo=UTC))
    assert local.hour == 21
    assert local.utcoffset() == timedelta(hours=6)


def test_of_reads_what_the_row_carries() -> None:
    cfg = ChatConfig.of(Chat(chat_id=1, tz_offset=-5, currency="USD"))
    assert cfg.tz_offset == -5
    assert cfg.currency == "USD"


def test_a_western_offset_is_negative_not_wrapped() -> None:
    cfg = ChatConfig(tz_offset=-5, currency="USD")
    assert cfg.localized(datetime(2026, 9, 9, 2, 0, tzinfo=UTC)).day == 8
