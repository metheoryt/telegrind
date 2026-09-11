"""Does the prompt actually work? Costs tokens; `-m llm` only."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from telegrind.config import ChatConfig
from telegrind.extract import build_prompt, drafts_from
from telegrind.llm import EXTRACT_SYSTEM, EXTRACT_TOOL, use_tool
from telegrind.models import KIND_TEXT, LoggedMessage

pytestmark = pytest.mark.llm

CFG = ChatConfig(tz_offset=6, currency="KZT")
CASES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "extraction.yaml").read_text()
)


def texts_of(case: dict) -> list[str]:
    """A case is one message or several. Several is how attribution is tested.

    A single-row window cannot catch a fact landing on the wrong message,
    because there is only one message it can land on.
    """
    return case["messages"] if "messages" in case else [case["message"]]


def rows_for(case: dict) -> list[LoggedMessage]:
    sent = datetime.fromisoformat(case.get("sent", "2026-09-11 12:00"))
    return [
        LoggedMessage(
            id=i,
            chat_pk=1,
            message_id=1000 + i,
            kind=KIND_TEXT,
            text=text,
            tg_date=sent.replace(tzinfo=CFG.tz),
            raw={},
        )
        for i, text in enumerate(texts_of(case), 1)
    ]


def is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@pytest.mark.parametrize("case", CASES, ids=lambda c: " / ".join(texts_of(c)))
async def test_the_corpus_still_extracts(case: dict) -> None:
    rows = rows_for(case)
    prompt = build_prompt(rows, [], "(пусто)", CFG, chat_id=1)
    payload = await use_tool(EXTRACT_SYSTEM, prompt, EXTRACT_TOOL)

    drafts, complaints = drafts_from(payload, rows, CFG)
    shape = [(d.message.message_id, d.kind, d.fields) for d in drafts]

    assert complaints == []
    assert drafts, "the message states a fact and none came back"

    if "date" in case:
        assert CFG.localized(drafts[0].at).date().isoformat() == case["date"]

    if case.get("numeric"):
        # The measurement bug: the quantity survived only inside a text
        # field, so no aggregate could ever reach it.
        assert any(is_number(v) for d in drafts for v in d.fields.values()), (
            f"ни одного числового поля: {shape}"
        )

    if "facts_per_message" in case:
        # Attribution: a fact belongs to the message that states it.
        got = [sum(1 for d in drafts if d.message is row) for row in rows]
        assert got == case["facts_per_message"], f"{got}: {shape}"
