from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from telegrind import store
from telegrind.extract import Draft
from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    SOURCE_TELEGRAM,
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICTS,
    Chat,
    Entry,
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
    """Enough of AsyncSession for the store helpers.

    `rows` is the default answer. `answers`, when given, is consumed one
    `execute` at a time — needed since `upsert_message` selects the message
    and then its entry.
    """

    def __init__(
        self, rows: list[object], answers: list[list[object]] | None = None
    ) -> None:
        self.rows = rows
        self.answers = answers
        self.statements: list[object] = []
        self.added: list[object] = []
        self._next_id = 0

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        if self.answers is not None and self.answers:
            return FakeResult(self.answers.pop(0))
        return FakeResult(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        # A real flush assigns a primary key to a pending row — the one
        # behaviour `test_a_first_sighting_writes_the_message_and_its_entry`
        # depends on to tell "the entry links row.id" from "the entry links
        # nothing", since both read as `None is None` otherwise.
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                self._next_id += 1
                obj.id = self._next_id


def entry(**overrides: object) -> Entry:
    base = {
        "id": 11,
        "chat_pk": 1,
        "source": SOURCE_TELEGRAM,
        "external_id": "4821",
        "message_pk": 7,
        "occurred_at": TG_DATE,
        "content": "4500 такси",
        "verdict": VERDICT_FACT,
    }
    base.update(overrides)
    return Entry(**base)


def fact(seq: int, deleted_at: datetime | None = None, kind: str = "expense") -> Fact:
    return Fact(
        chat_pk=1,
        entry_pk=7,
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

    count = await tombstone_facts(session, entry_pk=7, at=when)

    assert count == 2
    assert [r.deleted_at for r in rows] == [when, when]


async def test_tombstone_on_a_message_with_no_facts_reports_zero() -> None:
    session = FakeSession([])
    count = await tombstone_facts(
        session, entry_pk=7, at=datetime(2026, 9, 11, tzinfo=UTC)
    )
    assert count == 0


async def test_restore_clears_the_tombstone() -> None:
    when = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    rows = [fact(1, deleted_at=when)]
    count = await restore_facts(FakeSession(rows), entry_pk=7)

    assert count == 1
    assert rows[0].deleted_at is None


def logged(message_id: int, *, text: str | None = "x") -> LoggedMessage:
    """An unattached message row, enough for the window queries."""
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=TG_DATE + timedelta(minutes=message_id),
        raw={},
    )


async def test_unextracted_tail_asks_for_content_and_chat_order() -> None:
    rows = [entry(id=1), entry(id=2)]
    session = FakeSession(rows)

    tail = await store.unextracted_tail(session, chat_pk=1, limit=200)

    assert tail == rows
    rendered = str(session.statements[-1])
    assert "entry.extracted_at IS NULL" in rendered
    assert "entry.verdict" in rendered
    assert "trim" in rendered.lower()
    assert "LIMIT" in rendered


async def test_context_before_comes_back_oldest_first() -> None:
    # The query walks backwards from the pivot, so the driver hands them
    # back newest-first and the function has to flip them.
    session = FakeSession([entry(id=9), entry(id=8)])

    context = await store.context_before(
        session, chat_pk=1, pivot=entry(id=10), limit=10
    )

    assert [row.id for row in context] == [8, 9]


async def test_a_first_sighting_writes_the_message_and_its_entry() -> None:
    """Both rows, one transaction. The entry is what the queue reads, so a
    message stored without one is a message that is never extracted."""
    session = FakeSession([], answers=[[], []])
    chat = Chat(id=1, chat_id=7)

    row, ent, created = await store.upsert_message(
        session, chat, text_message(), verdict=VERDICT_FACT
    )

    assert created is True
    assert row in session.added
    assert ent in session.added
    assert ent.source == SOURCE_TELEGRAM
    assert ent.external_id == "4821"
    assert row.id is not None
    assert ent.message_pk is row.id
    assert ent.occurred_at == TG_DATE
    assert ent.content == "4500 такси"
    assert ent.verdict == VERDICT_FACT


async def test_the_external_id_is_the_telegram_message_id_as_text() -> None:
    """`(chat_pk, source, external_id)` is what makes a re-import idempotent,
    and it is text for every source — an int here would be a second spelling."""
    session = FakeSession([], answers=[[], []])
    _, ent, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), text_message(message_id=4821)
    )
    assert ent.external_id == "4821"
    assert isinstance(ent.external_id, str)


async def test_an_edit_puts_the_entry_back_in_the_queue() -> None:
    existing = logged(4821)
    ent = entry(extracted_at=AT, extract_error="boom", content="старый текст")
    session = FakeSession([], answers=[[existing], [ent]])

    _, got, created = await store.upsert_message(
        session,
        Chat(id=1, chat_id=7),
        text_message(text="4500 такси"),
        verdict=VERDICT_FACT,
    )

    assert created is False
    assert got is ent
    assert ent.extracted_at is None
    assert ent.extract_error is None
    assert ent.content == "4500 такси"


async def test_a_question_writes_its_verdict_onto_the_entry_not_the_message() -> None:
    session = FakeSession([], answers=[[], []])
    row, ent, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), text_message(), verdict=VERDICT_QUESTION
    )
    assert ent.verdict == VERDICT_QUESTION
    assert not hasattr(row, "verdict")


async def test_a_side_loaded_entry_is_in_the_queue() -> None:
    """The design's central claim: there is no second extraction path.

    An imported raw entry has no message and still gets extracted by the
    same pass as a typed one."""
    rows = [entry(id=12, message_pk=None, source="v1-receipts", external_id="r-1")]
    session = FakeSession(rows)

    tail = await store.unextracted_tail(session, chat_pk=1, limit=200)

    assert tail == rows
    # The fake returns rows unconditionally, so the return-value assertion
    # above cannot by itself tell "the query would include this row" from
    # "the query would silently exclude it and the fake wouldn't know" —
    # confirmed by deliberately adding `Entry.message_pk.is_not(None)` to
    # the where clause and watching this test keep passing. Assert on the
    # rendered statement too, which does discriminate.
    rendered = str(session.statements[-1])
    assert "message_pk IS NOT NULL" not in rendered


async def test_the_queue_excludes_a_structured_entry_for_both_reasons() -> None:
    """A substitution, and a deliberate one. The spec asks for each reason to
    be asserted alone; a fake session returns its rows unfiltered, so no unit
    test here can tell «excluded by the stamp» from «excluded by the empty
    content». What it can pin is that both predicates are in the statement.
    The per-reason check is run against a live database in the walkthrough."""
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[-1])

    # the stamp
    assert "entry.extracted_at IS NULL" in rendered
    # the empty content, trimmed
    assert "trim" in rendered.lower()
    assert "entry.content" in rendered
    # and the verdict, which is the third gate
    assert "entry.verdict" in rendered
    # read off `entry` alone: a join to `message` would drop every
    # side-loaded entry silently. Asserted on the statement's resolved FROM
    # list, not a text substring — a correlated WHERE-join
    # (`Entry.message_pk == LoggedMessage.id` with no `.join()` call)
    # compiles to `FROM entry, message`, which contains neither "from
    # message" nor "join message", and entry's own `message_pk` column
    # already defeats a bare "message" substring check. The FROM list is
    # the one thing no join spelling can hide from.
    froms = {t.name for t in session.statements[-1].get_final_froms()}
    assert froms == {"entry"}


async def test_the_queue_orders_by_when_things_happened() -> None:
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[-1])
    assert "ORDER BY entry.occurred_at, entry.id" in rendered


async def test_messages_for_loads_them_in_one_query_keyed_by_entry() -> None:
    """Explicit, never a relationship attribute: a lazy load on an
    AsyncSession raises MissingGreenlet and no fake session can see it."""
    msg = logged(4821)
    session = FakeSession([msg])

    got = await store.messages_for(session, [entry(id=11, message_pk=msg.id)])

    assert got == {11: msg}
    assert len(session.statements) == 1


async def test_messages_for_asks_nothing_when_no_entry_has_a_message() -> None:
    session = FakeSession([])
    got = await store.messages_for(session, [entry(id=11, message_pk=None)])
    assert got == {}
    assert session.statements == []


async def test_replace_facts_updates_in_place_and_tombstones_the_surplus() -> None:
    live = [fact(seq=1, kind="expense"), fact(seq=2, kind="expense")]
    session = FakeSession(live)
    row = entry(id=10)
    drafts = [
        Draft(entry=row, seq=1, kind="expense", at=AT, fields={"amount": 500}),
    ]

    written = await store.replace_facts(
        session,
        chat_pk=1,
        entry_pk=10,
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
    row = entry(id=10)
    drafts = [Draft(entry=row, seq=1, kind="expense", at=AT, fields={})]

    await store.replace_facts(
        session,
        chat_pk=1,
        entry_pk=10,
        drafts=drafts,
        model="m",
        prompt_version="v",
        now=AT,
    )

    assert len(session.added) == 1
    assert session.added[0].kind == "expense"


def test_mark_extracted_clears_a_previous_error() -> None:
    row = entry(id=10)
    row.extract_error = "boom"

    store.mark_extracted([row], model="m", prompt_version="v", at=AT)

    assert row.extracted_at == AT
    assert row.extract_model == "m"
    assert row.extract_error is None


def test_mark_failed_leaves_the_message_pending() -> None:
    row = entry(id=10)

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


async def test_the_tail_is_filtered_by_the_verdict() -> None:
    """A second flag that could disagree with the verdict is the bug the
    design forbids, which is why `extractable` was dropped rather than
    left unread (`4d60c7b65ad2`)."""
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[0].whereclause)
    assert "entry.verdict" in rendered
    assert "message.extractable" not in rendered


async def test_upsert_writes_the_verdict_on_a_new_row() -> None:
    session = FakeSession([], answers=[[]])
    _, ent, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_QUESTION
    )
    assert created is True
    assert ent.verdict == VERDICT_QUESTION


async def test_a_stored_q_is_not_a_fact() -> None:
    """The tail is selected by verdict from this commit on, and a /q row has
    content — so leaving it on the default verdict puts the user's own
    question into the extractor and coins a kind out of it. That is the
    taxonomy poisoning the whole design exists to prevent."""
    session = FakeSession([], answers=[[]])
    _, ent, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_QUESTION
    )
    assert ent.verdict != VERDICT_FACT


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
    )
    ent = entry(id=5, message_pk=42, verdict=VERDICT_FACT)
    session = FakeSession([], answers=[[existing], [ent]])
    _, got, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_SYSTEM
    )
    assert created is False
    assert got.verdict == VERDICT_SYSTEM
