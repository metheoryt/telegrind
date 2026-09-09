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
