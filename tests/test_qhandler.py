from types import SimpleNamespace

from telegrind.bot.handlers import query as handler
from telegrind.config import ChatConfig
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


async def _spec_for(question: str, words: str, cfg: ChatConfig, today: object) -> Spec:
    return SPEC


async def _query_run(session: object, chat_pk: int, spec: Spec) -> Answer:
    return Answer(rows=[Row(group=None, value=100.0, n=1)], skipped=0)


async def _render(question: str, spec: Spec, result: Answer, cfg: ChatConfig) -> str:
    return "Сто тенге."


def test_question_of_strips_the_command() -> None:
    assert handler.question_of("/q сколько я потратил") == "сколько я потратил"
    assert handler.question_of("/q@telegrind_bot сколько") == "сколько"
    assert handler.question_of("/q") == ""
    assert handler.question_of(None) == ""


async def test_an_empty_question_asks_for_one_and_runs_no_pass() -> None:
    text = await handler.answer_for(
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

    text = await handler.answer_for(
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

    text = await handler.answer_for(
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
    from telegrind.query import Unanswerable

    async def passes(session: object, chat: object, cfg: ChatConfig) -> SimpleNamespace:
        return report()

    async def refuses(
        question: str, words: str, cfg: ChatConfig, today: object
    ) -> Spec:
        raise Unanswerable("median")

    text = await handler.answer_for(
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

    assert "переформулируй" in text
