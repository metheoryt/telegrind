from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from telegrind.models import KIND_TEXT, KIND_VOICE, Fact
from telegrind.store import (
    message_kind,
    message_values,
    restore_facts,
    tombstone_facts,
)

TG_DATE = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)
LOCAL = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))


def text_message(**overrides: object) -> SimpleNamespace:
    base = {
        "message_id": 4821,
        "date": TG_DATE,
        "edit_date": None,
        "text": "4500 такси",
        "caption": None,
        "voice": None,
        "forward_origin": None,
    }
    base.update(overrides)
    return SimpleNamespace(
        model_dump=lambda mode=None: {"message_id": base["message_id"]}, **base
    )


def test_kind_of_a_text_message() -> None:
    assert message_kind(text_message()) == KIND_TEXT


def test_kind_of_a_voice_message() -> None:
    voice = SimpleNamespace(file_id="AwAC", duration=9)
    assert message_kind(text_message(text=None, voice=voice)) == KIND_VOICE


def test_values_lift_the_text() -> None:
    values = message_values(text_message())
    assert values["message_id"] == 4821
    assert values["kind"] == KIND_TEXT
    assert values["text"] == "4500 такси"
    assert values["tg_date"] == TG_DATE
    assert values["edited_at"] is None


def test_values_prefer_a_caption_when_there_is_no_text() -> None:
    values = message_values(text_message(text=None, caption="4500 такси"))
    assert values["text"] == "4500 такси"


def test_values_lift_the_voice_file_and_duration() -> None:
    voice = SimpleNamespace(file_id="AwACAgIAA", duration=9)
    values = message_values(text_message(text=None, voice=voice))
    assert values["kind"] == KIND_VOICE
    assert values["audio_file_id"] == "AwACAgIAA"
    assert values["audio_duration"] == 9
    assert values["text"] is None


def test_an_edit_timestamp_arrives_as_a_unix_int() -> None:
    """aiogram parses `date` into a datetime and leaves `edit_date` an int.

    Production sent 1789094740 straight into a timestamptz column and the
    whole edit died. The fake used to pass a datetime here, which is
    exactly why the suite stayed green while the bot did not.
    """
    values = message_values(text_message(edit_date=1789094740))
    assert values["edited_at"] == datetime(2026, 9, 11, 2, 45, 40, tzinfo=UTC)


def test_values_carry_the_edit_timestamp() -> None:
    edited = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)
    values = message_values(text_message(edit_date=edited))
    assert values["edited_at"] == edited


def test_values_use_the_forward_origin_date_when_present() -> None:
    origin_date = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    values = message_values(
        text_message(forward_origin=SimpleNamespace(date=origin_date))
    )
    assert values["tg_date"] == origin_date


def test_values_always_carry_a_raw_dump() -> None:
    assert message_values(text_message())["raw"] == {"message_id": 4821}


def test_values_never_carry_a_transcript_for_text() -> None:
    values = message_values(text_message())
    assert values["transcript"] is None
    assert values["transcript_model"] is None


class FakeResult:
    def __init__(self, rows: list[Fact]) -> None:
        self._rows = rows

    def scalars(self) -> list[Fact]:
        return self._rows


class FakeSession:
    """Enough of AsyncSession for the tombstone helpers.

    They only ever select and mutate attributes — nothing here flushes."""

    def __init__(self, rows: list[Fact]) -> None:
        self.rows = rows
        self.queries: list[object] = []

    async def execute(self, query: object) -> FakeResult:
        self.queries.append(query)
        return FakeResult(self.rows)


def fact(seq: int, deleted_at: datetime | None = None) -> Fact:
    return Fact(
        chat_pk=1,
        message_pk=7,
        seq=seq,
        kind="expense",
        at=TG_DATE,
        fields={"amount": 100},
        deleted_at=deleted_at,
    )


async def test_tombstone_stamps_every_live_fact() -> None:
    rows = [fact(1), fact(2)]
    session = FakeSession(rows)
    when = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    count = await tombstone_facts(session, message_pk=7, at=when)

    assert count == 2
    assert [r.deleted_at for r in rows] == [when, when]


async def test_tombstone_on_a_message_with_no_facts_reports_zero() -> None:
    session = FakeSession([])
    count = await tombstone_facts(
        session, message_pk=7, at=datetime(2026, 9, 11, tzinfo=UTC)
    )
    assert count == 0


async def test_restore_clears_the_tombstone() -> None:
    when = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    rows = [fact(1, deleted_at=when)]
    count = await restore_facts(FakeSession(rows), message_pk=7)

    assert count == 1
    assert rows[0].deleted_at is None


def test_values_do_not_carry_extractability() -> None:
    """extractable is a handler decision, not something lifted off the message."""
    assert "extractable" not in message_values(text_message())
