from datetime import UTC, datetime
from types import SimpleNamespace

from telegrind.config import ChatConfig
from telegrind.extract import Report, author_of, build_prompt, drafts_from, run
from telegrind.models import KIND_TEXT, LoggedMessage

CFG = ChatConfig(tz_offset=6, currency="KZT")
OWNER = 111


def logged(message_id: int, text: str, *, raw: dict | None = None) -> LoggedMessage:
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=datetime(2026, 9, 11, 3, 0, tzinfo=UTC),
        raw=raw or {},
    )


def test_a_plain_message_is_the_owner_talking() -> None:
    assert author_of(logged(1, "4500 такси"), OWNER) == "я"


def test_a_forward_of_my_own_message_is_still_me() -> None:
    row = logged(
        1,
        "4500 такси",
        raw={"forward_origin": {"type": "user", "sender_user": {"id": OWNER}}},
    )

    assert author_of(row, OWNER) == "я"


def test_a_forward_from_someone_else_names_them() -> None:
    row = logged(
        1,
        "верни 5000",
        raw={
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 222, "first_name": "Мама"},
            }
        },
    )

    assert author_of(row, OWNER) == "переслано от «Мама»"


def test_a_hidden_sender_is_its_own_third_case() -> None:
    row = logged(
        1,
        "верни 5000",
        raw={"forward_origin": {"type": "hidden_user", "sender_user_name": "Мама"}},
    )

    assert "неизвестно" in author_of(row, OWNER)


def test_a_channel_forward_names_the_channel() -> None:
    row = logged(
        1,
        "новый монитор",
        raw={"forward_origin": {"type": "channel", "chat": {"title": "Техника"}}},
    )

    assert author_of(row, OWNER) == "переслано из канала «Техника»"


def test_a_reply_names_its_parent_by_marker() -> None:
    prompt = build_prompt(
        tail=[
            logged(10, "макбук за 660000"),
            logged(11, "чек", raw={"reply_to_message": {"message_id": 10}}),
        ],
        context=[],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [1]" in prompt


def test_a_reply_to_a_context_message_names_its_context_marker() -> None:
    prompt = build_prompt(
        tail=[logged(11, "чек", raw={"reply_to_message": {"message_id": 9}})],
        context=[logged(9, "макбук за 660000")],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [C1]" in prompt


def test_a_reply_to_something_outside_the_window_says_so() -> None:
    prompt = build_prompt(
        tail=[logged(11, "чек", raw={"reply_to_message": {"message_id": 3}})],
        context=[],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "вне окна" in prompt


def test_the_prompt_numbers_the_tail_and_labels_the_context() -> None:
    prompt = build_prompt(
        tail=[logged(10, "4500 такси"), logged(11, "и ещё 300 кофе")],
        context=[logged(9, "вес 82.4")],
        taxonomy="- expense (5): amount, comment",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "[1]" in prompt and "[2]" in prompt
    assert "[C1]" in prompt
    # The chat's wall clock, not UTC's: 03:00 UTC is 09:00 in Almaty.
    assert "2026-09-11 09:00" in prompt
    assert "- expense (5): amount, comment" in prompt
    assert "KZT" in prompt


def test_a_fact_is_attributed_to_the_message_it_names() -> None:
    tail = [logged(10, "4500 такси"), logged(11, "и ещё 300 кофе")]
    payload = {
        "facts": [
            {"message": 2, "kind": "expense", "when": "", "fields": {"amount": "300"}}
        ]
    }

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert complaints == []
    assert len(drafts) == 1
    assert drafts[0].message is tail[1]
    # A number that parses becomes a real JSON number, so `(fields->>…)` works.
    assert drafts[0].fields == {"amount": 300}


def test_an_unparseable_amount_survives_as_text() -> None:
    tail = [logged(10, "около 500 на такси")]
    payload = {
        "facts": [
            {
                "message": 1,
                "kind": "expense",
                "when": "",
                "fields": {"amount": "около 500"},
            }
        ]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert drafts[0].fields == {"amount": "около 500"}


def test_when_is_resolved_against_the_messages_own_clock() -> None:
    # Sent 2026-09-11 09:00 Almaty. «вчера» is the 10th, not the day the
    # batch pass happens to run.
    tail = [logged(10, "41 бат массаж вчера")]
    payload = {
        "facts": [{"message": 1, "kind": "expense", "when": "вчера", "fields": {}}]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert CFG.localized(drafts[0].at).date().isoformat() == "2026-09-10"


def test_a_message_that_states_no_time_is_dated_by_the_message() -> None:
    tail = [logged(10, "4500 такси")]
    payload = {"facts": [{"message": 1, "kind": "expense", "fields": {}}]}

    drafts, _ = drafts_from(payload, tail, CFG)

    assert drafts[0].at == tail[0].tg_date


def test_a_fact_pointing_outside_the_window_becomes_a_complaint() -> None:
    tail = [logged(10, "4500 такси")]
    payload = {"facts": [{"message": 7, "kind": "expense", "fields": {}}]}

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert drafts == []
    assert len(complaints) == 1
    assert "7" in complaints[0]


def test_a_fact_with_no_kind_becomes_a_complaint() -> None:
    tail = [logged(10, "4500 такси")]
    payload = {"facts": [{"message": 1, "kind": "", "fields": {}}]}

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert drafts == []
    assert complaints


def test_seq_restarts_within_each_message() -> None:
    tail = [logged(10, "хлеб 500 и молоко 300")]
    payload = {
        "facts": [
            {"message": 1, "kind": "expense", "fields": {"comment": "хлеб"}},
            {"message": 1, "kind": "expense", "fields": {"comment": "молоко"}},
        ]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert [d.seq for d in drafts] == [1, 2]


class FakeWindowSession:
    """Enough session for `run`: two selects, then adds."""

    def __init__(self, tail: list, context: list, live_facts: list) -> None:
        self.results = [tail, context, live_facts]
        self.added: list[object] = []

    async def execute(self, statement: object) -> SimpleNamespace:
        rows = self.results.pop(0) if self.results else []
        return SimpleNamespace(scalars=lambda: iter(rows), all=lambda: [])

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


async def test_an_empty_tail_makes_no_call() -> None:
    called = False

    async def never(*args: object, **kwargs: object) -> dict:
        nonlocal called
        called = True
        return {}

    report = await run(
        FakeWindowSession([], [], []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=never,
    )

    assert report == Report(pending=0, extracted=0, facts=0, failed=0, complaints=0)
    assert not called


async def test_a_successful_pass_marks_every_message_including_the_silent_ones() -> (
    None
):
    tail = [logged(10, "4500 такси"), logged(11, "привет")]

    async def call(
        system: str, user: str, tool: dict, *, model: str | None = None
    ) -> dict:
        return {
            "facts": [{"message": 1, "kind": "expense", "fields": {"amount": "4500"}}]
        }

    report = await run(
        FakeWindowSession(tail, [], []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=call,
    )

    assert report.extracted == 2
    assert report.facts == 1
    assert all(row.extracted_at is not None for row in tail)
    assert all(row.extract_error is None for row in tail)


async def test_a_failed_call_leaves_the_tail_pending_and_countable() -> None:
    tail = [logged(10, "4500 такси")]

    async def boom(
        system: str, user: str, tool: dict, *, model: str | None = None
    ) -> dict:
        raise RuntimeError("503")

    report = await run(
        FakeWindowSession(tail, [], []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=boom,
    )

    assert report.failed == 1
    assert report.extracted == 0
    assert tail[0].extracted_at is None
    assert "503" in tail[0].extract_error
