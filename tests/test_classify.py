from typing import Any

from telegrind import classify
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICT_TALK,
)


def answering(verdict: str) -> Any:
    async def call(system: str, user: str, tool: dict, **kwargs: Any) -> dict:
        return {"verdict": verdict}

    return call


def test_q_is_a_question_without_a_call() -> None:
    assert classify.presumed("/q сколько я потратил") == VERDICT_QUESTION


def test_any_other_command_is_system_without_a_call() -> None:
    assert classify.presumed("/start") == VERDICT_SYSTEM


def test_a_message_with_nothing_to_read_is_a_fact_without_a_call() -> None:
    """A sticker or a photo. `_has_content()` keeps it out of the tail
    anyway, so this is today's behaviour at no cost."""
    assert classify.presumed(None) == VERDICT_FACT
    assert classify.presumed("   ") == VERDICT_FACT


def test_ordinary_text_needs_the_model() -> None:
    assert classify.presumed("4500 такси") is None


async def test_a_reply_into_a_talk_thread_needs_no_call() -> None:
    """The reply chain outranks the classifier.

    Measured in the live chat on 2026-09-13: «а последний коммит какой»,
    said as a reply to Claude's own answer, was classified `question` and
    answered by the SQL engine with «По этому вопросу записей нет.» It
    reads as a question about the ledger only when it is not said
    mid-conversation, and the chain is what knows the difference.
    """

    async def explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("a continuation needs no model call")

    assert (
        await classify.verdict_for(
            "а последний коммит какой", continues=VERDICT_TALK, call=explode
        )
        == VERDICT_TALK
    )


async def test_a_command_inside_a_talk_thread_is_still_a_command() -> None:
    """`presumed` runs first, and that order is chosen, not inherited: /q
    is the explicit override and must keep meaning «ask the ledger» even
    when it is typed as a reply to Claude."""

    async def explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("a command needs no model call")

    assert (
        await classify.verdict_for(
            "/q сколько я потратил", continues=VERDICT_TALK, call=explode
        )
        == VERDICT_QUESTION
    )


async def test_a_reply_into_any_other_thread_is_classified_normally() -> None:
    """Only `talk` is contagious. A reply to one's own fact is fact
    chaining, and a reply to a ledger answer is not a conversation."""
    assert (
        await classify.verdict_for(
            "4500 такси", continues=VERDICT_QUESTION, call=answering("fact")
        )
        == VERDICT_FACT
    )


async def test_the_model_decides_ordinary_text() -> None:
    assert await classify.verdict_for(
        "почему это расход", continues=None, call=answering("talk")
    ) == (VERDICT_TALK)


async def test_a_presumed_verdict_makes_no_call() -> None:
    async def explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("the classifier was called for a command")

    assert (
        await classify.verdict_for("/start", continues=None, call=explode)
        == VERDICT_SYSTEM
    )


async def test_a_classifier_failure_defaults_to_fact() -> None:
    """Storage is unconditional and must not come to depend on a model
    call: a hiccup costs a routing decision, never a message."""

    async def failing(*args: Any, **kwargs: Any) -> dict:
        raise RuntimeError("overloaded_error")

    assert (
        await classify.verdict_for("4500 такси", continues=None, call=failing)
        == VERDICT_FACT
    )


async def test_a_verdict_the_model_invented_defaults_to_fact() -> None:
    assert (
        await classify.verdict_for("x", continues=None, call=answering("чепуха"))
        == VERDICT_FACT
    )


async def test_a_payload_with_no_verdict_key_defaults_to_fact() -> None:
    async def empty(*args: Any, **kwargs: Any) -> dict:
        return {}

    assert await classify.verdict_for("x", continues=None, call=empty) == VERDICT_FACT


async def test_a_non_dict_payload_defaults_to_fact() -> None:
    """The seam the plan mandates for fakes is not private: a fake that
    returns None rather than raising must not escape as an
    AttributeError. This is the test that would have caught the gap."""

    async def nothing(*args: Any, **kwargs: Any) -> dict:
        return None  # type: ignore[return-value]

    assert await classify.verdict_for("x", continues=None, call=nothing) == VERDICT_FACT
