"""Requirement coverage (SPEC REQ-TEST-1)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_requirement_is_claimed_by_a_test():
    """REQ-TEST-1: every requirement id in SPEC.md appears in at least one test name, docstring or marker."""
    ids = set(re.findall(r"\*\*(REQ-[A-Z]+-\d+)\*\*", (ROOT / "SPEC.md").read_text()))
    tests = "\n".join(p.read_text() for p in (ROOT / "tests").rglob("*.py"))
    missing = sorted(i for i in ids if not re.search(rf"\b{re.escape(i)}\b", tests))
    assert ids, "SPEC.md names no requirement ids"
    assert missing == [], f"unclaimed: {missing}"
