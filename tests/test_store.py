from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from telegrind import store
from telegrind.extract import Draft
from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICTS,
    Chat,
    Fact,
    LoggedMessage,
)
from telegrind.store import (
    message_kind,
    message_values,
    restore_facts,
    tombstone_facts,
)

TG_DATE = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)
LOCAL = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))
AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


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

    def scalar_one_or_none(self) -> object | None:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Enough of AsyncSession for the tombstone and window helpers.

    They only ever select and mutate attributes. `added` is here for
    replace_facts, which is the one helper that inserts."""

    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def fact(seq: int, deleted_at: datetime | None = None, kind: str = "expense") -> Fact:
    return Fact(
        chat_pk=1,
        message_pk=7,
        seq=seq,
        kind=kind,
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


def logged(
    message_id: int, *, text: str | None = "x", extracted: bool = False
) -> LoggedMessage:
    """An unattached message row, enough for the window queries."""
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=datetime(2026, 9, 11, 12, message_id, tzinfo=UTC),
        raw={},
        extracted_at=datetime(2026, 9, 11, tzinfo=UTC) if extracted else None,
    )


async def test_unextracted_tail_asks_for_content_and_chat_order() -> None:
    rows = [logged(1), logged(2)]
    session = FakeSession(rows)

    tail = await store.unextracted_tail(session, chat_pk=1, limit=200)

    assert tail == rows
    rendered = str(session.statements[-1])
    assert "extracted_at IS NULL" in rendered
    assert "verdict" in rendered
    assert "trim" in rendered.lower()
    assert "LIMIT" in rendered


async def test_context_before_comes_back_oldest_first() -> None:
    # The query walks backwards from the pivot, so the driver hands them
    # back newest-first and the function has to flip them.
    session = FakeSession([logged(9), logged(8)])

    context = await store.context_before(session, chat_pk=1, pivot=logged(10), limit=10)

    assert [row.message_id for row in context] == [8, 9]


async def test_replace_facts_updates_in_place_and_tombstones_the_surplus() -> None:
    live = [fact(seq=1, kind="expense"), fact(seq=2, kind="expense")]
    session = FakeSession(live)
    row = logged(10)
    drafts = [
        Draft(message=row, seq=1, kind="expense", at=AT, fields={"amount": 500}),
    ]

    written = await store.replace_facts(
        session,
        chat_pk=1,
        message_pk=10,
        drafts=drafts,
        model="m",
        prompt_version="v",
        now=AT,
    )

    assert written == 1
    assert live[0].fields == {"amount": 500}
    assert live[0].deleted_at is None
    assert live[1].deleted_at == AT


async def test_replace_facts_adds_a_row_for_a_new_seq() -> None:
    session = FakeSession([])
    row = logged(10)
    drafts = [Draft(message=row, seq=1, kind="expense", at=AT, fields={})]

    await store.replace_facts(
        session,
        chat_pk=1,
        message_pk=10,
        drafts=drafts,
        model="m",
        prompt_version="v",
        now=AT,
    )

    assert len(session.added) == 1
    assert session.added[0].kind == "expense"


def test_mark_extracted_clears_a_previous_error() -> None:
    row = logged(10)
    row.extract_error = "boom"

    store.mark_extracted([row], model="m", prompt_version="v", at=AT)

    assert row.extracted_at == AT
    assert row.extract_model == "m"
    assert row.extract_error is None


def test_mark_failed_leaves_the_message_pending() -> None:
    row = logged(10)

    store.mark_failed([row], "boom")

    assert row.extracted_at is None
    assert row.extract_error == "boom"


def test_the_four_verdicts_are_closed() -> None:
    """Null would mean «not classified», «classifier failed» and «not a fact»
    all at once, which is undebuggable exactly when it misroutes."""
    assert VERDICTS == ("fact", "question", "talk", "system")


def test_reply_to_reads_the_parent_id_off_the_row() -> None:
    row = LoggedMessage(raw={"reply_to_message": {"message_id": 77}})
    assert store.reply_to(row) == 77


def test_reply_to_is_none_for_a_plain_message() -> None:
    assert store.reply_to(LoggedMessage(raw={})) is None
    assert store.reply_to(LoggedMessage(raw=None)) is None


def _msg(message_id: int = 4821) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        date=TG_DATE,
        edit_date=None,
        text="4500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=3260987),
        model_dump=lambda mode=None: {"message_id": message_id},
    )


async def test_the_tail_is_filtered_by_verdict_not_by_extractable() -> None:
    """`extractable` is still written, but a second flag that can disagree
    with the verdict is the bug the design forbids — so nothing reads it."""
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[0].whereclause)
    assert "message.verdict" in rendered
    assert "message.extractable" not in rendered


async def test_upsert_writes_the_verdict_on_a_new_row() -> None:
    session = FakeSession([None])
    row, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_QUESTION
    )
    assert created is True
    assert row.verdict == VERDICT_QUESTION


async def test_a_stored_q_is_not_a_fact() -> None:
    """The tail is selected by verdict from this commit on, and a /q row has
    content — so leaving it on the default verdict puts the user's own
    question into the extractor and coins a kind out of it. That is the
    taxonomy poisoning the whole design exists to prevent."""
    session = FakeSession([None])
    row, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_QUESTION
    )
    assert row.verdict != VERDICT_FACT


async def test_upsert_overwrites_the_verdict_on_an_edit() -> None:
    """An edit can move a message from one verdict to another; a stale
    verdict would route the corrected message the way the typo read."""
    existing = LoggedMessage(
        id=42,
        chat_pk=1,
        message_id=10,
        kind=KIND_TEXT,
        text="4500 такси",
        tg_date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        raw={},
        verdict=VERDICT_FACT,
    )
    session = FakeSession([existing])
    row, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_SYSTEM
    )
    assert created is False
    assert row.verdict == VERDICT_SYSTEM
