from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegrind import store
from telegrind.config import ChatConfig
from telegrind.extract import Report, author_of, build_prompt, drafts_from, run
from telegrind.models import (
    KIND_TEXT,
    SOURCE_TELEGRAM,
    Entry,
    LoggedMessage,
)

CFG = ChatConfig(tz_offset=6, currency="KZT")
OWNER = 111
TG_DATE = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)


def logged(message_id: int, text: str, *, raw: dict | None = None) -> LoggedMessage:
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=TG_DATE,
        raw=raw or {},
    )


def entry(entry_id: int, content: str, *, message_pk: int | None = None) -> Entry:
    return Entry(
        id=entry_id,
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id=str(entry_id),
        message_pk=message_pk,
        occurred_at=datetime(2026, 9, 11, 3, entry_id, tzinfo=UTC),
        content=content,
    )


def chat_entry(row: LoggedMessage) -> Entry:
    """The entry a chat message would get, mirroring `store._entry_for`.

    Its id equals the message's own id, so a `messages` dict keyed by
    `row.id` needs no separate bookkeeping in the tests below, and its
    `occurred_at` equals `row.tg_date`, so timestamp assertions carried
    over from before the entry split still hold.
    """
    return Entry(
        id=row.id,
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id=str(row.message_id),
        message_pk=row.id,
        occurred_at=row.tg_date,
        content=row.text,
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
    parent = logged(10, "макбук за 660000")
    child = logged(11, "чек", raw={"reply_to_message": {"message_id": 10}})
    prompt = build_prompt(
        tail=[chat_entry(parent), chat_entry(child)],
        context=[],
        messages={parent.id: parent, child.id: child},
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [1]" in prompt


def test_a_reply_to_a_context_message_names_its_context_marker() -> None:
    parent = logged(9, "макбук за 660000")
    child = logged(11, "чек", raw={"reply_to_message": {"message_id": 9}})
    prompt = build_prompt(
        tail=[chat_entry(child)],
        context=[chat_entry(parent)],
        messages={parent.id: parent, child.id: child},
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [C1]" in prompt


def test_a_reply_to_something_outside_the_window_says_so() -> None:
    child = logged(11, "чек", raw={"reply_to_message": {"message_id": 3}})
    prompt = build_prompt(
        tail=[chat_entry(child)],
        context=[],
        messages={child.id: child},
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "вне окна" in prompt


def test_the_prompt_numbers_the_tail_and_labels_the_context() -> None:
    m10 = logged(10, "4500 такси")
    m11 = logged(11, "и ещё 300 кофе")
    m9 = logged(9, "вес 82.4")
    prompt = build_prompt(
        tail=[chat_entry(m10), chat_entry(m11)],
        context=[chat_entry(m9)],
        messages={m10.id: m10, m11.id: m11, m9.id: m9},
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


async def test_a_side_loaded_entry_is_rendered_without_an_author() -> None:
    """A side-loaded entry has no author and no reply edge, so the prompt
    states neither. Inventing «я» would tell the model a bank statement was
    typed by the user."""
    ent = Entry(
        id=11,
        chat_pk=1,
        source="v1-expenses",
        external_id="4821",
        message_pk=None,
        occurred_at=TG_DATE,
        content="4500 такси",
    )

    prompt = build_prompt([ent], [], {}, "expense (3): amount", CFG, chat_id=OWNER)

    assert "4500 такси" in prompt
    assert "(я)" not in prompt
    # Not a bare "ответ на": that phrase also sits in the section's fixed
    # instructional text, present whenever the tail is non-empty. The
    # generated reply edge is always arrow-prefixed ("→ ответ на …"), and
    # the boilerplate never is — this is the substring that actually
    # distinguishes "no reply edge was rendered" from "the tail section
    # exists at all".
    assert "→ ответ на" not in prompt


async def test_a_chat_entry_still_states_its_author_and_reply_edge() -> None:
    """The one thing the hop must not lose: the reply edge is what lets two
    messages give one fact, and it is read off the message, not the entry."""
    parent = logged(10, "хлеб 500")
    child = logged(11, "и молоко 300", raw={"reply_to_message": {"message_id": 10}})
    first = entry(1, "хлеб 500", message_pk=parent.id)
    second = entry(2, "и молоко 300", message_pk=child.id)

    prompt = build_prompt(
        [first, second],
        [],
        {1: parent, 2: child},
        "expense (3): amount",
        CFG,
        chat_id=OWNER,
    )

    assert "(я)" in prompt
    assert "ответ на [1]" in prompt


def test_a_fact_is_attributed_to_the_message_it_names() -> None:
    tail = [entry(10, "4500 такси"), entry(11, "и ещё 300 кофе")]
    payload = {
        "facts": [
            {"message": 2, "kind": "expense", "when": "", "fields": {"amount": "300"}}
        ]
    }

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert complaints == []
    assert len(drafts) == 1
    assert drafts[0].entry is tail[1]
    # A number that parses becomes a real JSON number, so `(fields->>…)` works.
    assert drafts[0].fields == {"amount": 300}


def test_an_unparseable_amount_survives_as_text() -> None:
    tail = [entry(10, "около 500 на такси")]
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
    tail = [entry(10, "41 бат массаж вчера")]
    payload = {
        "facts": [{"message": 1, "kind": "expense", "when": "вчера", "fields": {}}]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert CFG.localized(drafts[0].at).date().isoformat() == "2026-09-10"


def test_a_message_that_states_no_time_is_dated_by_the_message() -> None:
    tail = [entry(10, "4500 такси")]
    payload = {"facts": [{"message": 1, "kind": "expense", "fields": {}}]}

    drafts, _ = drafts_from(payload, tail, CFG)

    assert drafts[0].at == tail[0].occurred_at


def test_a_fact_pointing_outside_the_window_becomes_a_complaint() -> None:
    tail = [entry(10, "4500 такси")]
    payload = {"facts": [{"message": 7, "kind": "expense", "fields": {}}]}

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert drafts == []
    assert len(complaints) == 1
    assert "7" in complaints[0]


def test_a_fact_with_no_kind_becomes_a_complaint() -> None:
    tail = [entry(10, "4500 такси")]
    payload = {"facts": [{"message": 1, "kind": "", "fields": {}}]}

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert drafts == []
    assert complaints


def test_seq_restarts_within_each_message() -> None:
    tail = [entry(10, "хлеб 500 и молоко 300")]
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


async def test_the_pass_loads_the_messages_it_needs_in_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never an attribute on a relationship — a lazy load on an AsyncSession
    raises MissingGreenlet at the access, and no fake session here can see
    it. The pass asks `store.messages_for` once, for the tail and the
    context together."""
    calls: list[list[Entry]] = []

    async def fake_messages_for(session: object, entries: list[Entry]) -> dict:
        calls.append(list(entries))
        return {}

    monkeypatch.setattr(store, "messages_for", fake_messages_for)

    tail = [entry(2, "4500 такси")]
    context = [entry(1, "хлеб 500")]

    async def call(
        system: str, user: str, tool: dict, *, model: str | None = None
    ) -> dict:
        return {"facts": []}

    await run(
        FakeWindowSession(tail, context, []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=call,
    )

    assert len(calls) == 1
    assert calls[0] == tail + context


async def test_a_successful_pass_marks_every_message_including_the_silent_ones() -> (
    None
):
    tail = [entry(10, "4500 такси"), entry(11, "привет")]

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
    tail = [entry(10, "4500 такси")]

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


def test_a_stored_bot_message_is_not_attributed_to_the_user() -> None:
    """The bot's own messages are in the window now. Left as «я» they read
    as the user asserting whatever the bot said.

    The fixture carries `from_user.is_bot`, because that is what the check
    reads. `verdict` cannot be it: the user's own non-`/q` commands are
    `system` too — see the test below.
    """
    row = LoggedMessage(raw={"from_user": {"is_bot": True}})
    assert author_of(row, chat_id=7) == "бот"


def test_a_users_own_command_is_not_attributed_to_the_bot() -> None:
    """`system` is two different authors. `classify.presumed` gives every
    non-`/q` slash command that verdict, and `store.context_before` applies
    no verdict filter — so a `/start` the *user* typed sits in the
    extraction window. Read off the verdict it is labelled «бот», and the
    prompt then tells the model the bot said it.

    `raw["from_user"]["is_bot"]` is the test that cannot be wrong about
    this: `outbound.say` stores what Telegram returned for a message the
    bot sent, and a user's command carries `is_bot: False`. The key is
    `from_user`, not the Bot API's `from` — `store.upsert_message` dumps
    with `model_dump(mode="json")` and no `by_alias=True`, so aiogram's
    field name is what lands in the column.
    """
    row = LoggedMessage(
        raw={"from_user": {"id": 555, "is_bot": False}},
        text="/start",
    )
    assert author_of(row, chat_id=555) == "я"
