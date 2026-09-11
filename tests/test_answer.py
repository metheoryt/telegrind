from datetime import date

import pytest

from telegrind.answer import render, spec_for
from telegrind.config import ChatConfig
from telegrind.query import Answer, Row, Spec, Unanswerable

CFG = ChatConfig(tz_offset=6, currency="KZT")


async def test_spec_for_passes_the_vocabulary_and_today_to_the_model() -> None:
    seen = {}

    async def call(
        system: str, user: str, tool: dict, *, model: str | None = None
    ) -> dict:
        seen["system"] = system
        seen["user"] = user
        return {"kinds": ["expense"], "aggregate": "sum", "field": "amount"}

    spec = await spec_for(
        "сколько я потратил", "- expense (5): amount", CFG, date(2026, 9, 11), call=call
    )

    assert spec == Spec(kinds=("expense",), aggregate="sum", field="amount")
    assert "2026-09-11" in seen["system"]
    assert "- expense (5): amount" in seen["user"]


async def test_a_question_the_spec_cannot_express_raises() -> None:
    async def call(
        system: str, user: str, tool: dict, *, model: str | None = None
    ) -> dict:
        return {"aggregate": "median", "field": "amount"}

    with pytest.raises(Unanswerable):
        await spec_for("медиана", "", CFG, date(2026, 9, 11), call=call)


async def test_render_hands_the_model_the_numbers_it_must_not_recompute() -> None:
    seen = {}

    async def say(system: str, user: str, *, model: str | None = None) -> str:
        seen["user"] = user
        return "За август 12 300 KZT."

    text = await render(
        "сколько я потратил в августе",
        Spec(kinds=("expense",), aggregate="sum", field="amount"),
        Answer(rows=[Row(group=None, value=12300.0, n=4)], skipped=0),
        CFG,
        say=say,
    )

    assert text == "За август 12 300 KZT."
    assert "12300" in seen["user"]
    assert "KZT" in seen["user"]


async def test_render_tells_the_model_about_the_rows_it_could_not_add_up() -> None:
    async def say(system: str, user: str, *, model: str | None = None) -> str:
        assert "3" in user
        return "ok"

    await render(
        "сколько",
        Spec(kinds=("expense",), aggregate="sum", field="amount"),
        Answer(rows=[Row(group=None, value=100.0, n=1)], skipped=3),
        CFG,
        say=say,
    )


async def test_render_says_plainly_when_there_is_nothing() -> None:
    async def say(system: str, user: str, *, model: str | None = None) -> str:
        return "unused"

    text = await render(
        "сколько",
        Spec(kinds=("expense",), aggregate="sum", field="amount"),
        Answer(rows=[], skipped=0),
        CFG,
        say=say,
    )

    assert "нет" in text.lower()
