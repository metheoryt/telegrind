"""The fakes themselves: a turn and an ask through televibe against the fake claude (REQ-TEST-3)."""

import sqlite3

from conftest import records, turn_env
from televibe import Account, ClaudeCode, Done, Engine
from tgkit import user_message

from telegrind.incoming import incoming
from telegrind.log import Log


async def test_fake_turn_and_ask(tmp_path, fake_path):
    """REQ-TEST-3: the fake replays a turn (Done) and a one-shot call, and records its cwd and prompt."""
    claude = ClaudeCode(Account(tmp_path / "acc"))
    cwd = tmp_path / "u"
    cwd.mkdir()
    session = claude.new_session(cwd)
    async with (
        Engine(tmp_path / "state") as engine,
        engine.turn("разбери", session=session, env=turn_env(tmp_path)) as turn,
    ):
        end = [e async for e in turn][-1]
    assert isinstance(end, Done)
    assert end.text
    answer = await claude.ask("clean this", schema={"type": "object"}, env=turn_env(tmp_path))
    assert isinstance(answer.data, dict)
    recs = records(tmp_path)
    assert [r["prompt"] for r in recs] == ["разбери", "clean this"]
    assert recs[0]["cwd"] == str(cwd)


async def test_fake_pass_mode_writes_facts(tmp_path, fake_path):
    """REQ-TEST-3: FAKE_MODE=pass leaves facts.sqlite with processed rows and an expenses row per amount-led entry."""
    cwd = tmp_path / "u"
    cwd.mkdir()
    log = Log(cwd / "log.sqlite")
    log.record_received(incoming(user_message(text="4500 такси", message_id=10), now=1))
    log.record_received(incoming(user_message(text="позвонил маме", message_id=11), now=2))
    log.record_bot_sent(500, "ok", reply_to=10, raw={}, at=3)
    claude = ClaudeCode(Account(tmp_path / "acc"))
    session = claude.new_session(cwd)
    env = turn_env(tmp_path, mode="pass")
    async with Engine(tmp_path / "state") as engine, engine.turn("разбери", session=session, env=env) as turn:
        assert isinstance([e async for e in turn][-1], Done)
    facts = sqlite3.connect(cwd / "facts.sqlite")
    assert facts.execute("SELECT entry_id, revision FROM processed ORDER BY entry_id").fetchall() == [(10, 1), (11, 1)]
    assert facts.execute("SELECT entry_id, amount, comment FROM expenses").fetchall() == [(10, 4500.0, "такси")]
    log.close()
