import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telegrind import query
from telegrind.bot.answering import EMPTY_QUESTION, REFUSAL, answer_for, question_of
from telegrind.bot.handlers import query as handler
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_SYSTEM, Entry, LoggedMessage
from telegrind.query import Answer, Row, Spec

CFG = ChatConfig(tz_offset=6, currency="KZT")
CHAT = SimpleNamespace(id=1, chat_id=7)
SPEC = Spec(kinds=("expense",), aggregate="sum", field="amount")


def report(**kwargs: int) -> SimpleNamespace:
    base = {"pending": 0, "extracted": 0, "facts": 0, "failed": 0, "complaints": 0}
    return SimpleNamespace(**(base | kwargs))


async def _unreachable(*args: object, **kwargs: object) -> object:
    raise AssertionError("should not have been called")


async def _vocabulary(session: object, chat_pk: int) -> str:
    return "- expense (5): amount"


async def _no_vocabulary(*args: object, **kwargs: object) -> str:
    return ""


async def _spec_for(question: str, words: str, cfg: ChatConfig, today: object) -> Spec:
    return SPEC


async def _query_run(session: object, chat_pk: int, spec: Spec) -> Answer:
    return Answer(rows=[Row(group=None, value=100.0, n=1)], skipped=0)


async def _render(question: str, spec: Spec, result: Answer, cfg: ChatConfig) -> str:
    return "Сто тенге."


def test_question_of_strips_the_command() -> None:
    assert question_of("/q сколько я потратил") == "сколько я потратил"
    assert question_of("/q@telegrind_bot сколько") == "сколько"
    assert question_of("/q") == ""
    assert question_of(None) == ""


async def test_an_empty_question_asks_for_one_and_runs_no_pass() -> None:
    text = await answer_for(
        question="",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=_unreachable,
        spec_for=_unreachable,
        query_run=_unreachable,
        render=_unreachable,
        vocabulary=_unreachable,
    )

    assert "спроси" in text.lower()


async def test_the_answer_is_the_rendered_prose() -> None:
    async def passes(session: object, chat: object, cfg: ChatConfig) -> SimpleNamespace:
        return report(pending=2, extracted=2, facts=2)

    text = await answer_for(
        question="сколько",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=passes,
        spec_for=_spec_for,
        query_run=_query_run,
        render=_render,
        vocabulary=_vocabulary,
    )

    assert text == "Сто тенге."


async def test_a_pass_that_failed_is_reported_alongside_the_answer() -> None:
    async def passes(session: object, chat: object, cfg: ChatConfig) -> SimpleNamespace:
        return report(pending=3, failed=3)

    text = await answer_for(
        question="сколько",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=passes,
        spec_for=_spec_for,
        query_run=_query_run,
        render=_render,
        vocabulary=_vocabulary,
    )

    assert text.startswith("Сто тенге.")
    assert "3 сообщени" in text


async def test_a_question_the_spec_cannot_express_is_refused_honestly() -> None:
    """A refusal comes back as None, not as the refusal text: only the
    caller — `ask`, or Task 5's routing — decides what a `None` becomes."""

    async def passes(session: object, chat: object, cfg: ChatConfig) -> SimpleNamespace:
        return report()

    async def refuses(
        question: str, words: str, cfg: ChatConfig, today: object
    ) -> Spec:
        raise query.Unanswerable("median")

    result = await answer_for(
        question="медиана",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=passes,
        spec_for=refuses,
        query_run=_unreachable,
        render=_unreachable,
        vocabulary=_vocabulary,
    )

    assert result is None


async def test_an_unanswerable_question_returns_none_not_prose() -> None:
    """The caller has to be able to hand it to Claude instead."""

    async def refusing(*args: object, **kwargs: object) -> object:
        raise query.Unanswerable("unknown aggregate ''")

    async def no_pass(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(failed=0, complaints=0)

    result = await answer_for(
        "почему ты записал это расходом",
        CHAT,
        CFG,
        object(),
        passes=no_pass,
        spec_for=refusing,
        vocabulary=_no_vocabulary,
    )
    assert result is None


def test_the_refusal_text_is_still_available_to_the_caller() -> None:
    assert REFUSAL == "Не понял вопрос, переформулируй."


def test_a_question_survives_the_move_without_its_command() -> None:
    assert question_of("/q сколько я потратил") == "сколько я потратил"
    assert question_of("сколько я потратил") == "сколько я потратил"


class AskSession:
    """Enough session for `ask` end to end: every lookup finds nothing, so
    `upsert_message` always inserts, and the pending-tail count comes back
    non-empty so both `outbound.say` calls in `ask` fire."""

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
            scalars=lambda: iter([object()]),
        )

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


class AskBot:
    """A `Bot` fake that answers both the receipt reaction and a send."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.reactions: list[tuple[int, int, list[str]]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(
            message_id=900 + len(self.sent),
            date=datetime(2026, 9, 12, 10, tzinfo=UTC),
            edit_date=None,
            text=text,
            caption=None,
            voice=None,
            forward_origin=None,
            chat=SimpleNamespace(id=chat_id),
            model_dump=lambda mode="json": {"text": text},
        )

    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: list[Any]
    ) -> None:
        self.reactions.append((chat_id, message_id, [r.emoji for r in reaction]))


def ask_message(message_id: int = 777) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        edit_date=None,
        text="/q",
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=CHAT.chat_id),
        model_dump=lambda mode="json": {"text": "/q"},
    )


async def test_ask_sends_the_answer_as_a_reply_to_the_question() -> None:
    """A follow-up only resolves because the answer goes out as a reply to
    the question that triggered it — this pins `reply_to=message.message_id`
    against a regression that drops it or reverts to `bot.send_message`.

    Sending correctly is not enough: a regression that bypasses
    `outbound.say` for a direct `bot.send_message(..., reply_parameters=...)`
    would still pass a send-only check, and would silently stop storing the
    bot's own messages — the whole point of Task 2. So this also asserts
    what actually reached the session, not just what reached the bot.
    """
    session = AskSession()
    bot = AskBot()
    message = ask_message()

    await handler.ask(message, CHAT, CFG, session, bot)

    # The "разбираю N сообщений" notice and the answer both went out.
    assert len(bot.sent) == 2
    # And no receipt: /q is a question, and 💔 promises that tapping it
    # deletes the facts on the message — a question has none. The override
    # takes routing's path, so it cannot differ from a plain question.
    assert bot.reactions == []
    # Each upsert_message call now adds a message and its entry — the
    # verdict lives on the entry, not the message row.
    messages = [obj for obj in session.added if isinstance(obj, LoggedMessage)]
    entries = [obj for obj in session.added if isinstance(obj, Entry)]
    assert messages[0].receipt_emoji is None
    assert bot.sent[0].get("reply_parameters") is None
    assert bot.sent[1]["reply_parameters"].message_id == message.message_id

    # Both went through outbound.say, not a bare bot.send_message: the
    # question itself is stored first (verdict=question), then the two
    # outbound sends land as system rows.
    stored = [(m.text, e.verdict) for m, e in zip(messages, entries, strict=True)]
    assert stored[1:] == [
        ("Разбираю 1 сообщений…", VERDICT_SYSTEM),
        (EMPTY_QUESTION, VERDICT_SYSTEM),
    ]
