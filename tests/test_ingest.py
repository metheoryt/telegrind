import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telegrind import extract
from telegrind.bot.handlers import handlers
from telegrind.bot.handlers.handlers import (
    RECEIPT_CYCLE,
    RECEIPT_EMOJI,
    next_receipt,
)
from telegrind.bot.handlers.receipts import HANDED_OVER, acknowledge
from telegrind.config import ChatConfig
from telegrind.models import (
    KIND_TEXT,
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICT_TALK,
    Chat,
    LoggedMessage,
)

CFG = ChatConfig(tz_offset=6, currency="KZT")


class FakeBot:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, int, list[str]]] = []

    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: list[Any]
    ) -> None:
        self.reactions.append((chat_id, message_id, [r.emoji for r in reaction]))


def message(message_id: int = 4821) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 9, 9, 15, 40, tzinfo=UTC),
        edit_date=None,
        text="4500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=3260987),
        reply_to_message=None,
        model_dump=lambda mode=None: {"message_id": message_id},
    )


def test_the_receipt_is_a_broken_heart() -> None:
    assert RECEIPT_EMOJI == "💔"


async def test_acknowledge_sets_exactly_one_reaction() -> None:
    """A bot that sets two gets REACTIONS_TOO_MANY."""
    bot = FakeBot()
    await acknowledge(bot, chat_id=3260987, message_id=4821, emoji=RECEIPT_EMOJI)
    assert bot.reactions == [(3260987, 4821, ["💔"])]


async def test_acknowledge_survives_a_telegram_failure() -> None:
    """The receipt is cosmetic; the message row is already committed."""

    class Failing(FakeBot):
        async def set_message_reaction(
            self, chat_id: int, message_id: int, reaction: list[Any]
        ) -> None:
            raise RuntimeError("Bad Request: REACTION_INVALID")

    await acknowledge(Failing(), chat_id=1, message_id=2, emoji=RECEIPT_EMOJI)


def test_every_emoji_in_the_cycle_is_a_broken_or_burning_heart() -> None:
    """Any user reaction deletes, so the cycle must not stop looking like one.

    Telegram will not let the picker be narrowed, so the emoji the bot
    places is the only thing telling the user what a tap does. All three
    were checked against setMessageReaction on 2026-09-11.
    """
    assert RECEIPT_CYCLE == ("💔", "❤\u200d🔥", "💘")
    assert RECEIPT_CYCLE[0] == RECEIPT_EMOJI


def test_an_edit_advances_the_cycle() -> None:
    assert next_receipt("💔") == "❤\u200d🔥"
    assert next_receipt("❤\u200d🔥") == "💘"


def test_the_cycle_wraps() -> None:
    assert next_receipt("💘") == RECEIPT_CYCLE[0]


def test_a_message_from_before_the_column_is_assumed_to_carry_the_default() -> None:
    """Rows stored by the previous version have no receipt_emoji.

    They visibly carry 💔, because that is all the old code ever placed,
    so the first edit must move off it rather than re-place it.
    """
    assert next_receipt(None) == "❤\u200d🔥"


def test_an_emoji_that_is_no_longer_in_the_cycle_still_changes() -> None:
    """The point of the reaction is that an edit looks different."""
    assert next_receipt("👍") != "👍"


class EditSession:
    """Enough session for record_edited: one lookup, reused by the upsert.

    It keeps a transaction depth for the same reason `NewMessageSession`
    does — a real session raises «a transaction is already begun» on a
    nested `session.begin()`, and a fake with no transaction state at all
    is how that bug ships green. `record_edited` opens three at most and
    never overlaps them, which is the rule the module docstring states.

    `scalars` is here because the tombstone arm reads the message's live
    facts; an empty iterator keeps it out of the way of whatever each test
    is actually pinning.
    """

    def __init__(self, existing: LoggedMessage | None) -> None:
        self.existing = existing
        self.added: list[LoggedMessage] = []
        self.depth = 0
        self.commits = 0

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            assert self.depth == 0, "a transaction is already begun"
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
                self.commits += 1

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        row = self.existing
        return SimpleNamespace(
            scalar_one_or_none=lambda: row,
            scalars=lambda: iter(()),
        )

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def stored(*, extracted: bool, verdict: str = VERDICT_FACT) -> LoggedMessage:
    return LoggedMessage(
        id=42,
        chat_pk=1,
        message_id=10,
        kind=KIND_TEXT,
        text="4500 такси",
        tg_date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        raw={},
        receipt_emoji=RECEIPT_EMOJI,
        extracted_at=datetime(2026, 9, 11, 4, tzinfo=UTC) if extracted else None,
        verdict=verdict,
    )


def edit() -> SimpleNamespace:
    """An edited message. Its `chat.id` deliberately differs from the
    `Chat.chat_id` every test passes alongside it: the receipt and its
    clearing must be addressed by the row the middleware resolved, and the
    two are the same number in production, so only the fixture can tell a
    swap apart."""
    return SimpleNamespace(
        message_id=10,
        chat=SimpleNamespace(id=3260987),
        reply_to_message=None,
        text="5500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        edit_date=1789094740,
        model_dump=lambda mode="json": {},
    )


async def _noop_route(*args: Any, **kwargs: Any) -> None:
    """`route` is pinned by tests/test_routing.py; here it is in the way."""


async def _a_fact(*args: Any, **kwargs: Any) -> str:
    """A stand-in classifier. Every edit test needs one now that the edit
    path re-classifies — an unpatched `verdict_for` on text without a
    leading slash is a live Anthropic call, and conftest loads `.env`."""
    return VERDICT_FACT


async def test_an_edit_of_an_extracted_message_re_extracts_it(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(handlers.classify, "verdict_for", _a_fact)
    called: list[int] = []

    async def fake_run_for(
        session: object, chat: object, cfg: object, row: Any, **kwargs: object
    ) -> SimpleNamespace:
        called.append(row.id)
        return SimpleNamespace(facts=1, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=True)),
        CFG,
        FakeBot(),
    )

    assert called == [42]


async def test_an_edit_of_an_unextracted_message_makes_no_call(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(handlers.classify, "verdict_for", _a_fact)
    called: list[int] = []

    async def fake_run_for(
        session: object, chat: object, cfg: object, row: Any, **kwargs: object
    ) -> SimpleNamespace:
        called.append(row.id)
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=False)),
        CFG,
        FakeBot(),
    )

    assert called == []


async def test_editing_a_command_leaves_it_out_of_the_extractor(
    monkeypatch: Any,
) -> None:
    """`classify.presumed` reads the `/q` without a model call, and the
    question verdict is what keeps the row out of the tail."""
    monkeypatch.setattr(handlers, "route", _noop_route)
    called: list[int] = []

    async def fake_run_for(
        session: object, chat: object, cfg: object, row: Any, **kwargs: object
    ) -> SimpleNamespace:
        called.append(row.id)
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=False)
    existing.text = "/q сколкьо я потратил"
    message = edit()
    message.text = "/q сколько я потратил"

    await handlers.record_edited(
        message, Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert existing.verdict == VERDICT_QUESTION
    assert called == []


class NewMessageSession:
    """Enough session for `record`: no existing row, so upsert_message
    always inserts.

    Unlike the other fakes this one keeps a transaction depth. A real
    session raises «a transaction is already begun» on a nested
    `session.begin()`, and a fake with no transaction state at all is how
    that bug ships green — so this one refuses to nest, and `commits`
    counts how many transactions the handler actually opened.
    """

    def __init__(self) -> None:
        self.added: list[LoggedMessage] = []
        self.depth = 0
        self.commits = 0

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            assert self.depth == 0, "a transaction is already begun"
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
                self.commits += 1

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def command_message(text: str = "/start") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=99,
        date=datetime(2026, 9, 9, 15, 40, tzinfo=UTC),
        edit_date=None,
        text=text,
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=3260987),
        reply_to_message=None,
        model_dump=lambda mode=None: {"message_id": 99},
    )


class ReplySession(NewMessageSession):
    """`record` for a reply: the chain's rows exist, this message does not."""

    def __init__(self, rows: dict[int, Any]) -> None:
        super().__init__()
        self.rows = rows

    async def execute(self, statement: Any) -> SimpleNamespace:
        wanted = statement.compile().params.get("message_id_1")
        return SimpleNamespace(scalar_one_or_none=lambda: self.rows.get(wanted))


def chain_row(
    message_id: int, verdict: str, *, is_bot: bool, parent: int | None
) -> Any:
    return SimpleNamespace(
        message_id=message_id,
        verdict=verdict,
        raw={
            "from_user": {"is_bot": is_bot},
            **({"reply_to_message": {"message_id": parent}} if parent else {}),
        },
    )


async def test_a_reply_to_claude_is_talk_without_asking_the_classifier(
    monkeypatch: Any,
) -> None:
    """The chain measured live on 2026-09-13. 1124 was `talk`, 1125 is
    Claude's reply to it, and 1126 - «а последний коммит какой» - went to
    the SQL engine because the classifier only ever saw the text."""

    async def explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("a continuation needs no model call")

    # The real classifier, with an exploding model call: what is under
    # test here is the handler resolving the chain and handing it over,
    # and a stubbed `verdict_for` would test the stub.
    real = handlers.classify.verdict_for

    async def guarded(text: str | None, *, continues: str | None) -> str:
        return await real(text, continues=continues, call=explode)

    monkeypatch.setattr(handlers.classify, "verdict_for", guarded)
    monkeypatch.setattr(handlers, "route", _noop_route)

    session = ReplySession(
        {
            1125: chain_row(1125, VERDICT_SYSTEM, is_bot=True, parent=1124),
            1124: chain_row(1124, VERDICT_TALK, is_bot=False, parent=None),
        }
    )
    reply = message(1126)
    reply.text = "а последний коммит какой"
    reply.reply_to_message = SimpleNamespace(message_id=1125)

    await handlers.record(reply, Chat(id=1, chat_id=7), CFG, session, FakeBot())

    assert session.added[0].verdict == VERDICT_TALK


async def test_a_non_q_command_gets_the_system_verdict() -> None:
    """/start, /help and a typo all reach the one catch-all now that the
    COMMAND_LIKE filter is gone — `classify.presumed` is what keeps them
    out of the tail, and it does it without spending a model call. Leaving
    these on the fact verdict would put them right back in the tail."""
    session = NewMessageSession()
    bot = FakeBot()

    await handlers.record(
        command_message("/start"), Chat(id=1, chat_id=7), CFG, session, bot
    )

    assert session.added[0].verdict == VERDICT_SYSTEM
    # No receipt: nothing was recorded as a fact, so there is nothing to
    # promise a tap would delete.
    assert session.added[0].receipt_emoji is None
    assert bot.reactions == []


async def test_a_plain_message_still_gets_the_fact_verdict(monkeypatch: Any) -> None:
    """The fact path is what shipped before the classifier existed, right
    down to the 💔 that goes on as soon as the row is committed."""

    async def a_fact(*args: Any, **kwargs: Any) -> str:
        return VERDICT_FACT

    monkeypatch.setattr(handlers.classify, "verdict_for", a_fact)

    session = NewMessageSession()
    bot = FakeBot()
    await handlers.record(message(), Chat(id=1, chat_id=7), CFG, session, bot)

    assert session.added[0].verdict == VERDICT_FACT
    assert session.added[0].receipt_emoji == RECEIPT_EMOJI
    # Addressed by `chat.chat_id`, the row the middleware resolved from
    # this very update — not by `message.chat.id`. They are the same number
    # in production; the fixture keeps them apart so a swap is visible.
    assert bot.reactions == [(7, 4821, [RECEIPT_EMOJI])]


async def test_the_row_is_committed_before_the_classifier_runs(
    monkeypatch: Any,
) -> None:
    """The plan's first invariant, and the fakes can almost see it.

    aiogram advances the polling offset as it dispatches and runs handlers
    fire-and-forget, so a restart or a hung Anthropic call inside `record`
    loses an update that is never redelivered. The classifier is the only
    part of `record` that can hang, so the row has to be committed before
    it — and the row it commits has to be a plain `fact`, because that is
    what the message stays if the process dies right here.

    The `depth == 0` assertion is the other half of the same constraint: a
    write transaction must not be held open across a model call.
    """
    seen: list[tuple[int, str, int]] = []
    session = NewMessageSession()

    async def classifier(*args: Any, **kwargs: Any) -> str:
        row = session.added[0]
        seen.append((len(session.added), row.verdict, session.depth))
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", classifier)

    await handlers.record(message(), Chat(id=1, chat_id=7), CFG, session, FakeBot())

    # One row, already a fact, already out of any transaction.
    assert seen == [(1, VERDICT_FACT, 0)]
    # And only then refined, in a second transaction of its own.
    assert session.added[0].verdict == VERDICT_TALK
    assert session.commits == 2


async def test_a_message_the_classifier_leaves_alone_costs_one_transaction(
    monkeypatch: Any,
) -> None:
    """A fact is already what the first transaction wrote, so there is
    nothing to refine and no second round trip."""

    async def a_fact(*args: Any, **kwargs: Any) -> str:
        return VERDICT_FACT

    monkeypatch.setattr(handlers.classify, "verdict_for", a_fact)

    session = NewMessageSession()
    await handlers.record(message(), Chat(id=1, chat_id=7), CFG, session, FakeBot())

    assert session.commits == 1


async def test_a_question_gets_no_receipt(monkeypatch: Any) -> None:
    async def question(*args: Any, **kwargs: Any) -> str:
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", question)
    routed: list[str] = []

    async def fake_route(*args: Any, **kwargs: Any) -> None:
        routed.append(args[1].verdict)

    monkeypatch.setattr(handlers, "route", fake_route)

    session = EditSession(None)
    bot = FakeBot()
    await handlers.record(message(), Chat(id=1, chat_id=7), CFG, session, bot)

    assert routed == [VERDICT_QUESTION]
    assert session.added[0].verdict == VERDICT_QUESTION
    assert session.added[0].receipt_emoji is None
    assert bot.reactions == []


async def test_talk_is_stored_and_kept_out_of_the_extraction_tail(
    monkeypatch: Any,
) -> None:
    """«Nothing yet» is the queue, visible: no receipt, no reply, and the
    row is there for the meta layer to pick up."""

    async def talk(*args: Any, **kwargs: Any) -> str:
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", talk)

    session = NewMessageSession()
    bot = FakeBot()
    await handlers.record(message(), Chat(id=1, chat_id=7), CFG, session, bot)

    assert session.added[0].verdict == VERDICT_TALK
    assert bot.reactions == []


async def test_editing_a_q_row_does_not_flip_its_verdict(monkeypatch: Any) -> None:
    """The edit re-derives the verdict now rather than preserving it, and it
    has to land back on VERDICT_QUESTION.

    `classify.presumed` is what makes that true and what makes it free: the
    edited text still opens with `/q`, so the same rule that gave the row its
    verdict in the first place gives it again, with no model call. The old
    fact/system default a naive re-derivation would produce is the failure
    this pins.
    """
    monkeypatch.setattr(handlers, "route", _noop_route)

    async def fake_run_for(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=False, verdict=VERDICT_QUESTION)
    existing.text = "/q сколкьо я потратил"
    edited = edit()
    edited.text = "/q сколько я потратил"

    await handlers.record_edited(
        edited, Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert existing.verdict == VERDICT_QUESTION


async def test_editing_a_question_does_not_paint_a_receipt_on_it(
    monkeypatch: Any,
) -> None:
    """Ingest stopped putting 💔 on a question; the edit path has to agree.

    Otherwise editing a question advances a cycle it was never in and lands
    ❤‍🔥 on a row with no facts — a bubble promising that tapping it deletes
    them. The receipt must never claim something that did not happen.

    The clearing call below is not a receipt and is not painting one: it is
    the empty reaction list, and it is unconditional on an edit that earns
    no receipt because the column is not proof of what the bubble shows —
    see `test_a_released_row_edited_into_a_question_loses_the_stale_marker`.
    What this test pins is that no *emoji* is placed.
    """

    async def a_question(*args: Any, **kwargs: Any) -> str:
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", a_question)
    monkeypatch.setattr(handlers, "route", _noop_route)

    async def fake_run_for(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=False, verdict=VERDICT_QUESTION)
    existing.receipt_emoji = None
    edited = edit()
    edited.text = "сколько я потратил на такси"
    bot = FakeBot()

    await handlers.record_edited(
        edited, Chat(id=1, chat_id=7), EditSession(existing), CFG, bot
    )

    assert bot.reactions == [(7, 10, [])]
    assert existing.receipt_emoji is None


async def test_editing_a_fact_still_advances_its_receipt(monkeypatch: Any) -> None:
    """The guard is on the verdict, not on edits — an edited fact must
    still look different, which is the only signal the bot saw the edit.

    `route` is deliberately the real one here: the advanced emoji reaches
    Telegram through its `receipt=` keyword, so a handler that computed the
    right emoji and then let `route` re-place 💔 would pass every other
    assertion in this file.
    """
    monkeypatch.setattr(handlers.classify, "verdict_for", _a_fact)

    async def fake_run_for(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(facts=1, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=True)
    bot = FakeBot()

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, bot
    )

    assert existing.receipt_emoji == next_receipt(RECEIPT_EMOJI)
    assert bot.reactions == [(7, 10, [next_receipt(RECEIPT_EMOJI)])]


async def test_editing_a_message_with_no_prior_row_keeps_the_fact_default(
    monkeypatch: Any,
) -> None:
    """A row upsert_message has never seen before takes whatever the
    classifier makes of the edited text — here, the ordinary fact."""
    monkeypatch.setattr(handlers.classify, "verdict_for", _a_fact)

    async def fake_run_for(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    session = EditSession(None)
    await handlers.record_edited(edit(), Chat(id=1, chat_id=7), session, CFG, FakeBot())

    assert session.added[0].verdict == VERDICT_FACT
    # And the default receipt, not the next emoji along: there is no
    # previous sighting to have shown the user a 💔 already.
    assert session.added[0].receipt_emoji == RECEIPT_EMOJI


async def test_an_edit_after_the_handover_is_ignored(monkeypatch: Any) -> None:
    """👀 is the point of no return. The row is still overwritten — nothing
    written is ever lost — but nothing downstream reacts to it."""
    called: list[str] = []

    async def loud(*args: Any, **kwargs: Any) -> str:
        called.append("classified")
        return VERDICT_FACT

    monkeypatch.setattr(handlers.classify, "verdict_for", loud)

    async def fake_route(*args: Any, **kwargs: Any) -> None:
        called.append("routed")

    monkeypatch.setattr(handlers, "route", fake_route)

    existing = stored(extracted=False)
    existing.verdict = VERDICT_TALK
    existing.receipt_emoji = HANDED_OVER

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert called == []
    assert existing.text == "5500 такси"


async def test_the_handover_receipt_survives_the_edit_that_it_ignores(
    monkeypatch: Any,
) -> None:
    """The marker is the state. Clearing it would make the next edit
    classifiable again and hand the same message over twice."""

    async def loud(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("a handed-over message must not be re-classified")

    monkeypatch.setattr(handlers.classify, "verdict_for", loud)
    monkeypatch.setattr(handlers, "route", _noop_route)

    existing = stored(extracted=False)
    existing.verdict = VERDICT_TALK
    existing.receipt_emoji = HANDED_OVER
    bot = FakeBot()

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, bot
    )

    assert existing.receipt_emoji == HANDED_OVER
    assert existing.verdict == VERDICT_TALK
    assert bot.reactions == []


async def test_an_edit_that_turns_a_fact_into_talk_clears_the_receipt(
    monkeypatch: Any,
) -> None:
    async def talk(*args: Any, **kwargs: Any) -> str:
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", talk)
    monkeypatch.setattr(handlers, "route", _noop_route)

    existing = stored(extracted=False)
    bot = FakeBot()
    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, bot
    )

    assert bot.reactions == [(7, 10, [])]
    assert existing.receipt_emoji is None


async def test_an_edit_that_stays_a_fact_advances_the_cycle(
    monkeypatch: Any,
) -> None:
    async def fact(*args: Any, **kwargs: Any) -> str:
        return VERDICT_FACT

    monkeypatch.setattr(handlers.classify, "verdict_for", fact)
    monkeypatch.setattr(handlers, "route", _noop_route)

    existing = stored(extracted=False)
    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )
    assert existing.receipt_emoji == "❤‍🔥"


async def test_an_edited_question_is_answered_again(monkeypatch: Any) -> None:
    """The typo-fix case. Without it, correcting a question gets neither a
    reaction nor an answer — the hole steps 1 and 3 were folded to close."""

    async def question(*args: Any, **kwargs: Any) -> str:
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", question)
    routed: list[str] = []

    async def fake_route(*args: Any, **kwargs: Any) -> None:
        routed.append(args[1].verdict)

    monkeypatch.setattr(handlers, "route", fake_route)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=False)),
        CFG,
        FakeBot(),
    )
    assert routed == [VERDICT_QUESTION]


async def test_an_edited_caption_is_classified_from_the_caption(
    monkeypatch: Any,
) -> None:
    """The caption cousin, on the edit path.

    A photo of a receipt captioned «сколько я потратил на это» has no
    `.text`. Classifying from `.text` alone would hand the classifier None,
    which `presumed` answers `fact` without a call — so the corrected
    question would silently become a fact, collect 💔 and enter the
    extraction tail instead of being answered. Whatever decides the verdict
    must read the same words `route` answers from.
    """
    seen: list[str | None] = []

    async def classifier(text: str | None, *args: Any, **kwargs: Any) -> str:
        seen.append(text)
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", classifier)
    monkeypatch.setattr(handlers, "route", _noop_route)

    edited = edit()
    edited.text = None
    edited.caption = "сколько я потратил на это"

    await handlers.record_edited(
        edited,
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=False)),
        CFG,
        FakeBot(),
    )

    assert seen == ["сколько я потратил на это"]


async def test_an_edit_that_stops_being_a_fact_tombstones_what_it_derived(
    monkeypatch: Any,
) -> None:
    """The facts came from text that no longer exists, and nothing else will
    ever clear them.

    `store.unextracted_tail` selects on the verdict, so the row has just
    left the tail for good; `replace_facts` — the helper that tombstones
    what an edit dropped — is reachable only from an extraction pass. Left
    alone, `/q` goes on counting an expense whose message now reads «а
    почему это вообще расход», and the 💔 that offered to delete it was
    taken off the bubble by this very edit.
    """

    async def talk(*args: Any, **kwargs: Any) -> str:
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", talk)
    monkeypatch.setattr(handlers, "route", _noop_route)

    async def never(*args: object, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("a row that is no longer a fact must not be extracted")

    monkeypatch.setattr(extract, "run_for", never)

    tombstoned: list[int] = []

    async def fake_tombstone(session: Any, message_pk: int, at: Any) -> int:
        tombstoned.append(message_pk)
        return 1

    monkeypatch.setattr(handlers.store, "tombstone_facts", fake_tombstone)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=True)),
        CFG,
        FakeBot(),
    )

    assert tombstoned == [42]


async def test_an_edit_after_a_failed_re_extraction_still_retires_the_facts(
    monkeypatch: Any,
) -> None:
    """`extracted_at` is not «has facts», and this is the gap between them.

    Edit an extracted fact and the pass reruns; if its model call raises,
    `store.mark_failed` writes `extract_error` and leaves `extracted_at`
    None — deliberately, so the row stays pending and the next /q retries
    it — while the *previous* pass's facts are still live. Edit it again
    before any pass succeeds and a `was_extracted` guard reads False, skips
    this arm, and the row leaves the tail for good with the stale facts on
    it. The predicate has to be «we have seen this message before».
    """

    async def talk(*args: Any, **kwargs: Any) -> str:
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", talk)
    monkeypatch.setattr(handlers, "route", _noop_route)

    tombstoned: list[int] = []

    async def fake_tombstone(session: Any, message_pk: int, at: Any) -> int:
        tombstoned.append(message_pk)
        return 1

    monkeypatch.setattr(handlers.store, "tombstone_facts", fake_tombstone)

    # The state mark_failed leaves behind: pending again, error recorded,
    # and the facts of the last successful pass untouched.
    existing = stored(extracted=False)
    existing.extract_error = "APIConnectionError: Connection error."

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert tombstoned == [42]


async def test_an_edit_of_a_message_we_have_never_stored_tombstones_nothing(
    monkeypatch: Any,
) -> None:
    """No previous row, so there is nothing whose facts could be stale —
    and `row.id` is not assigned until the flush, so asking would be asking
    about the wrong thing."""

    async def talk(*args: Any, **kwargs: Any) -> str:
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", talk)
    monkeypatch.setattr(handlers, "route", _noop_route)

    async def never(session: Any, message_pk: int, at: Any) -> int:
        raise AssertionError("we have never seen this message, so it has no facts")

    monkeypatch.setattr(handlers.store, "tombstone_facts", never)

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(None), CFG, FakeBot()
    )


async def test_a_talk_row_edited_into_a_fact_gets_the_default_receipt(
    monkeypatch: Any,
) -> None:
    """💔 first, not ❤‍🔥. The cycle is «you already saw this one and I saw
    your edit», and a row that was never a fact has never been in it — so
    advancing unconditionally would show the second emoji on a message
    whose facts are being recorded for the first time."""

    monkeypatch.setattr(handlers.classify, "verdict_for", _a_fact)
    monkeypatch.setattr(handlers, "route", _noop_route)

    async def never(*args: object, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("nothing was extracted, so there is nothing to redo")

    monkeypatch.setattr(extract, "run_for", never)

    existing = stored(extracted=False, verdict=VERDICT_TALK)
    existing.receipt_emoji = None

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert existing.receipt_emoji == RECEIPT_EMOJI


async def test_a_released_row_edited_into_a_question_loses_the_stale_marker(
    monkeypatch: Any,
) -> None:
    """The startup sweep clears the column and cannot touch the bubble.

    A turn killed mid-flight leaves 👀 on screen and 👀 in the column; the
    sweep at the next boot takes the column back to NULL so the message is
    editable again, and there is no API to take a reaction off a message
    the bot is not currently handling — so the row and the screen disagree
    until something re-places or clears it. An edit is that something, and
    two of the three verdicts already cover it: `talk` is handed over again
    and re-places 👀, `fact` paints 💔 over it.

    A question is the third, and it answers itself — `route` says the
    number and places nothing — so the 👀 would survive with no turn behind
    it, claiming the message is with Claude while the answer sits under it.
    The receipt must never claim something that did not happen, so the
    column going to NULL has to reach the bubble too. `was_fact` cannot see
    this: a released row's column is already NULL, so nothing about it
    remembers that a bubble was ever placed.
    """

    async def a_question(*args: Any, **kwargs: Any) -> str:
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", a_question)
    monkeypatch.setattr(handlers, "route", _noop_route)

    # What the sweep leaves behind: talk, handed over once, marker cleared.
    released = stored(extracted=False, verdict=VERDICT_TALK)
    released.receipt_emoji = None
    bot = FakeBot()

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(released), CFG, bot
    )

    assert bot.reactions == [(7, 10, [])]
    assert released.receipt_emoji is None
