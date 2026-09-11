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


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["message"])
async def test_the_corpus_still_extracts(case: dict) -> None:
    sent = case.get("sent", "2026-09-11 12:00")
    row = LoggedMessage(
        id=1,
        chat_pk=1,
        message_id=1,
        kind=KIND_TEXT,
        text=case["message"],
        tg_date=datetime.fromisoformat(sent).replace(tzinfo=CFG.tz),
        raw={},
    )
    prompt = build_prompt([row], [], "(пусто)", CFG, chat_id=1)
    payload = await use_tool(EXTRACT_SYSTEM, prompt, EXTRACT_TOOL)

    drafts, complaints = drafts_from(payload, [row], CFG)

    assert complaints == []
    assert drafts, "the message states a fact and none came back"
    if "date" in case:
        assert CFG.localized(drafts[0].at).date().isoformat() == case["date"]
