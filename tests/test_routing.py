import contextlib
from types import SimpleNamespace
from typing import Any

from telegrind.bot import routing
from telegrind.bot.answering import REFUSAL
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICT_TALK,
    Chat,
    LoggedMessage,
)

CFG = SimpleNamespace(tz_offset=6, currency="KZT")


class FakeSession:
    """The one from tests/test_outbound.py, plus `scalars`.

    `route` counts `store.unextracted_tail` before it answers, and that
    reads `result.scalars()` — an empty iterator, so the pending notice
    stays out of the way of what each test is actually pinning.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            yield

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: iter(()),
        )

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


class Recorder:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, str | None]] = []
        self.said: list[tuple[str, int | None]] = []
        self.handed: list[int] = []

    async def acknowledge(
        self, bot: Any, chat_id: int, message_id: int, emoji: str
    ) -> None:
        self.reactions.append((message_id, emoji))

    async def clear_receipt(self, bot: Any, chat_id: int, message_id: int) -> None:
        self.reactions.append((message_id, None))

    async def say(
        self,
        bot: Any,
        session: Any,
        chat: Any,
        text: str,
        *,
        reply_to: int | None = None,
    ) -> Any:
        self.said.append((text, reply_to))
        return SimpleNamespace(message_id=901)


def row(verdict: str) -> LoggedMessage:
    return LoggedMessage(id=42, chat_pk=1, message_id=10, verdict=verdict, raw={})


def msg() -> SimpleNamespace:
    return SimpleNamespace(message_id=10, text="…", chat=SimpleNamespace(id=7))


async def test_a_fact_gets_the_receipt_and_no_words(monkeypatch: Any) -> None:
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    await routing.route(
        msg(), row(VERDICT_FACT), Chat(id=1, chat_id=7), CFG, object(), object()
    )

    assert rec.reactions == [(10, RECEIPT_EMOJI)]
    assert rec.said == []


async def test_a_question_is_answered_and_gets_no_receipt(monkeypatch: Any) -> None:
    """💔 promises that tapping deletes the facts on the message. A question
    has none, so the receipt would promise a gesture that does nothing."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def answered(*args: Any, **kwargs: Any) -> str:
        return "12 400 ₸."

    monkeypatch.setattr(routing, "answer_for", answered)

    await routing.route(
        msg(),
        row(VERDICT_QUESTION),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
    )

    assert rec.reactions == []
    assert rec.said == [("12 400 ₸.", 10)]


async def test_a_refused_question_falls_through_to_the_handler(
    monkeypatch: Any,
) -> None:
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def refused(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routing, "answer_for", refused)

    async def hand_over(*args: Any, **kwargs: Any) -> bool:
        rec.handed.append(10)
        return True

    await routing.route(
        msg(),
        row(VERDICT_QUESTION),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
        hand_over=hand_over,
    )

    assert rec.handed == [10]
    assert rec.said == []


async def test_a_refused_question_still_says_so_when_there_is_nobody_to_ask(
    monkeypatch: Any,
) -> None:
    """Without this arm, folding steps 1 and 3 does not close the hole it
    was folded to close: the question would get silence."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def refused(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routing, "answer_for", refused)

    await routing.route(
        msg(),
        row(VERDICT_QUESTION),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
        hand_over=None,
    )

    assert rec.said == [(REFUSAL, 10)]


async def test_a_question_the_meta_layer_also_declined_still_gets_the_refusal(
    monkeypatch: Any,
) -> None:
    """A hand-off that comes back False is the same as no hand-off at all:
    the question was asked, so something has to answer it."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def refused(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routing, "answer_for", refused)

    async def declines(*args: Any, **kwargs: Any) -> bool:
        return False

    await routing.route(
        msg(),
        row(VERDICT_QUESTION),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
        hand_over=declines,
    )

    assert rec.said == [(REFUSAL, 10)]


async def test_the_pending_notice_precedes_the_answer(monkeypatch: Any) -> None:
    """The first question after a quiet week pays for the week, and saying
    so is the only thing between the user and an unexplained pause."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def answered(*args: Any, **kwargs: Any) -> str:
        return "12 400 ₸."

    monkeypatch.setattr(routing, "answer_for", answered)

    class Pending(FakeSession):
        async def execute(self, statement: object) -> SimpleNamespace:
            return SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: iter([object(), object()]),
            )

    await routing.route(
        msg(), row(VERDICT_QUESTION), Chat(id=1, chat_id=7), CFG, Pending(), object()
    )

    assert rec.said == [("Разбираю 2 сообщений…", None), ("12 400 ₸.", 10)]


async def test_talk_with_nobody_to_hand_it_to_stays_bare(monkeypatch: Any) -> None:
    """«Nothing yet» is the queue, visible — and here the queue is waiting
    on a runtime that is not configured."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    await routing.route(
        msg(),
        row(VERDICT_TALK),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
        hand_over=None,
    )

    assert rec.reactions == []
    assert rec.said == []


async def test_talk_is_handed_over_when_there_is_somebody_to_ask(
    monkeypatch: Any,
) -> None:
    """And the receipt is not placed here: the meta layer puts 👀 on when
    the turn starts, so the bare messages are the ones not yet seen."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def hand_over(*args: Any, **kwargs: Any) -> bool:
        rec.handed.append(10)
        return True

    await routing.route(
        msg(),
        row(VERDICT_TALK),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
        hand_over=hand_over,
    )

    assert rec.handed == [10]
    assert rec.reactions == []
    assert rec.said == []


async def test_a_system_row_is_stored_and_nothing_else(monkeypatch: Any) -> None:
    """The bot's own messages come through here too, and a reply to one of
    them would be a loop."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def hand_over(*args: Any, **kwargs: Any) -> bool:
        rec.handed.append(10)
        return True

    await routing.route(
        msg(),
        row(VERDICT_SYSTEM),
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        object(),
        hand_over=hand_over,
    )

    assert rec.reactions == []
    assert rec.said == []
    assert rec.handed == []
