"""On-demand extraction eval. Costs tokens, so it is marked and deselected.

Run with: uv run pytest -m llm
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from telegrind.llm import extract
from telegrind.registry import SEED_CATEGORIES, Registry
from telegrind.sheets import Config

CASES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "extraction.yaml").read_text()
)
REG = Registry(SEED_CATEGORIES)
CFG = Config(dt_offset=6, currency="KZT")
NOW = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set"
    ),
]


@pytest.mark.parametrize(
    ("message", "expected"),
    [(c["message"], c["category"]) for c in CASES],
    ids=[c["message"] for c in CASES],
)
async def test_message_lands_in_the_expected_category(
    message: str, expected: str
) -> None:
    facts, _ = await extract(message, REG, CFG, NOW)
    assert facts, f"nothing extracted from {message!r}"
    assert facts[0].category == expected


async def test_a_multi_fact_message_returns_both() -> None:
    facts, _ = await extract("4500 такси и вес 82.4", REG, CFG, NOW)
    assert {f.category for f in facts} == {"expense", "telemetry"}


DATED = [c for c in CASES if c.get("date")]


@pytest.mark.parametrize(
    ("message", "expected"),
    [(c["message"], c["date"]) for c in DATED],
    ids=[c["message"] for c in DATED],
)
async def test_the_date_field_resolves_to_the_expected_day(
    message: str, expected: str
) -> None:
    """Tense must beat the column's past bias.

    `Дата` is a `date` column, so `_DATE_DESCRIPTION` tells the model an
    *ambiguous* reference resolves backwards. A message in the future tense
    is not ambiguous, and must not be dragged into the past — this is what
    makes a future-dated fact expressible without a second column type.
    """
    facts, _ = await extract(message, REG, CFG, NOW)
    assert facts, f"nothing extracted from {message!r}"
    raw = facts[0].fields.get("Дата")
    assert isinstance(raw, str), f"no Дата in {facts[0].fields!r}"
    assert datetime.fromisoformat(raw).date().isoformat() == expected
