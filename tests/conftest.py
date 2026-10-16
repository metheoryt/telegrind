"""Shared fixtures: the fake claude on PATH, fixture paths, a turn env."""

import json
import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
FAKES_BIN = TESTS / "fakes" / "bin"
FIXTURES = TESTS / "fixtures" / "claude"
sys.path.insert(0, str(TESTS))  # `from tgkit import ...` in tests


@pytest.fixture
def fake_path(monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKES_BIN}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def turn_env(
    tmp_path: Path, *, mode: str = "", turn: str = "turn.jsonl", ask: str = "ask.jsonl", **knobs: object
) -> dict[str, str]:
    env = {
        "FAKE_TURN_FIXTURE": str(FIXTURES / turn),
        "FAKE_ASK_FIXTURE": str(FIXTURES / ask),
        "FAKE_RECORD_DIR": str(tmp_path / "records"),
    }
    if mode:
        env["FAKE_MODE"] = mode
    env.update({f"FAKE_{k.upper()}": str(v) for k, v in knobs.items()})
    return env


def records(tmp_path: Path) -> list[dict]:
    d = tmp_path / "records"
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)]
