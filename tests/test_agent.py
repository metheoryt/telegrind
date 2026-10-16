"""Agent turns through televibe against the fake claude (SPEC section 7, REQ-TEST-3)."""

import asyncio
import os
import subprocess
import sys
import textwrap

import pytest
from conftest import records, turn_env
from televibe import Engine
from tgkit import NOW, FakeBot, bot_message, user_message

from telegrind.agent import NO_AGENT_TEXT, QUOTA_TEXT, Agent, make_provider
from telegrind.app import App
from telegrind.config import Config
from telegrind.layout import Layout

ANSWER = "televibe fixture"  # the `result` text of tests/fixtures/claude/turn.jsonl


def config(tmp_path, **env):
    return Config.from_env(
        {
            "BOT_TOKEN": "t",
            "ALLOWED_USER_IDS": "7,8",
            "DATA_DIR": str(tmp_path / "data"),
            "ACCOUNT_HOME": str(tmp_path / "acc"),
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            **env,
        }
    )


@pytest.fixture
async def rig(tmp_path, fake_path):
    cfg = config(tmp_path)
    async with Engine(tmp_path / "state", max_concurrent=cfg.max_concurrent) as engine:
        app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: int(NOW.timestamp()))
        agent = Agent(config=cfg, app=app, engine=engine, provider=make_provider(cfg), turn_env=turn_env(tmp_path))
        app.answerer = agent
        yield app, agent, engine
        await app.drain()
        app.close()


async def test_question_turn_answers_links_and_stores(rig, tmp_path):
    """REQ-TURN-1, REQ-TURN-2, REQ-TURN-3, REQ-TURN-6, REQ-TURN-8, REQ-LOG-7.

    The fake's answer is sent, linked to the chain, stored; reactions 👀 → 👨‍💻 → cleared."""
    app, agent, _ = rig
    agent.turn_env = {**turn_env(tmp_path), "BOT_TOKEN": "x", "ELEVENLABS_API_KEY": "y"}  # never reach the child
    await app.on_message(user_message(text="сколько такси?"))
    await app.drain()
    bot, store = app.bot, app.log_for(7)
    assert [r[1] for r in bot.reactions()] == ["👀", "👨‍💻", None]
    texts = bot.texts()
    assert len(texts) == 1 and ANSWER in texts[0]
    sent_id = store.conn.execute("SELECT message_id FROM entries WHERE author = 'bot'").fetchone()[0]
    assert store.entry(sent_id).reply_to == 10 and await store.chain_of(7, sent_id) is not None
    assert store.entry(sent_id).text == ANSWER
    assert store.turns_since("question", 0) == 1
    rec = records(tmp_path)[0]
    assert rec["cwd"] == str(app.layout.user_dir(7)) and rec["prompt"] == "сколько такси?"
    argv = rec["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions" and "--session-id" in argv
    assert "2026-10-15" in argv[argv.index("--append-system-prompt") + 1]
    assert (
        "BOT_TOKEN" not in rec["env"]
        and "ELEVENLABS_API_KEY" not in rec["env"]
        and rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    )
    assert rec["env"]["DISABLE_AUTOUPDATER"] == "1"


async def test_reply_to_the_answer_resumes_the_session_with_the_quoted_text(rig, tmp_path):
    """REQ-TURN-3, REQ-TURN-2: a reply to the answer continues the chain (--resume same id), quoting the answer."""
    app, _, _ = rig
    await app.on_message(user_message(text="сколько такси?"))
    await app.drain()
    sent_id = (
        app.bot.sends()[0]
        and app.log_for(7).conn.execute("SELECT message_id FROM entries WHERE author = 'bot'").fetchone()[0]
    )
    await app.on_message(
        user_message(text="а за сентябрь", message_id=11, reply_to=bot_message(message_id=sent_id, text=ANSWER))
    )
    await app.drain()
    first, second = records(tmp_path)
    assert (
        first["argv"][first["argv"].index("--session-id") + 1] == second["argv"][second["argv"].index("--resume") + 1]
    )
    assert ANSWER in second["argv"][second["argv"].index("--append-system-prompt") + 1]


async def test_new_question_is_a_new_session(rig, tmp_path):
    """REQ-TURN-3: a question that replies to nothing starts a new chain and a new session."""
    app, *_ = rig
    await app.on_message(user_message(text="сколько такси?"))
    await app.on_message(user_message(text="а сколько еды?", message_id=11))
    await app.drain()
    ids = [r["argv"][r["argv"].index("--session-id") + 1] for r in records(tmp_path)]
    assert len(ids) == 2 and ids[0] != ids[1]


async def test_without_a_token_the_bot_says_it_cannot(tmp_path, fake_path):
    """REQ-TURN-1: no CLAUDE_CODE_OAUTH_TOKEN → fixed sentence, no turn, no process."""
    cfg = config(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="")
    assert make_provider(cfg) is None
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir))
    agent = Agent(config=cfg, app=app, engine=None, provider=None)
    app.answerer = agent
    await app.on_message(user_message(text="сколько?"))
    await app.drain()
    assert (
        app.bot.texts() == [NO_AGENT_TEXT]
        and records(tmp_path) == []
        and app.log_for(7).turns_since("question", 0) == 0
    )
    app.close()


async def test_quota_is_per_local_day(rig, tmp_path):
    """REQ-TURN-8: over TURN_QUOTA question turns since local midnight, the fixed sentence and no turn."""
    app, agent, _ = rig
    agent.config = config(tmp_path, TURN_QUOTA="1")
    await app.on_message(user_message(text="раз?"))
    await app.drain()
    await app.on_message(user_message(text="два?", message_id=11))
    await app.drain()
    assert len(records(tmp_path)) == 1 and app.bot.texts()[-1] == QUOTA_TEXT.format(quota=1)
    assert agent.quota_left(7) == 0
    app.log_for(7).conn.execute("UPDATE turns SET at = at - 86400")  # yesterday's turns do not count
    assert agent.quota_left(7) == 1


async def test_pass_posts_the_digest_only_when_processed_changed(rig, tmp_path):
    """REQ-TURN-4, REQ-TURN-7: a pass that wrote processed posts Done.text; one that changed nothing posts nothing."""
    app, agent, _ = rig
    await app.on_message(user_message(text="4500 такси"))
    agent.turn_env = turn_env(tmp_path, mode="pass", sleep=0.2)  # the work lands after Started, as in a real pass
    await agent.run_pass(7)
    assert len(app.bot.texts()) == 1 and app.bot.texts()[0] == ANSWER
    rec = records(tmp_path)[0]
    assert "--session-id" in rec["argv"] and rec["prompt"].startswith("Разбери")
    assert app.log_for(7).turns_since("pass", 0) == 1 and app.log_for(7).unprocessed(agent.facts(7).processed()) == []
    agent.turn_env = turn_env(tmp_path)  # a plain replay changes no table
    await agent.run_pass(7)
    assert len(app.bot.texts()) == 1


class SignalBot(FakeBot):
    """A FakeBot that sets `queued` when the 👀 of a question lands."""

    def __init__(self):
        super().__init__()
        self.queued = asyncio.Event()

    async def set_message_reaction(self, **kwargs) -> bool:
        await super().set_message_reaction(**kwargs)
        if kwargs["reaction"] and kwargs["reaction"][0].emoji == "👀":
            self.queued.set()
        return True


async def test_pass_queued_behind_a_question_measures_from_its_own_start(rig, tmp_path):
    """REQ-TURN-7: a question ahead in the lane runs the pass the contract asks of it; the pass queued behind it
    derives nothing, so it posts nothing — `before` is measured when the pass starts, not when it is queued."""
    app, agent, _ = rig
    app.bot = SignalBot()
    agent.turn_env = turn_env(tmp_path, mode="pass", sleep=0.3)  # the question writes processed
    await app.on_message(user_message(text="4500 такси"))
    await app.on_message(user_message(text="сколько такси?", message_id=11))
    await app.bot.queued.wait()  # the question's env is taken before its task next yields
    agent.turn_env = turn_env(tmp_path)  # the pass is a plain replay: it changes nothing
    assert agent.start_pass(7) is True
    await agent.passes[7]
    await app.drain()
    assert len(records(tmp_path)) == 2
    assert agent.facts(7).mark() != (0, 0)  # the question did change processed
    assert app.bot.texts() == [ANSWER]  # the question's answer, and no digest


async def test_failed_pass_posts_nothing(rig, tmp_path):
    """REQ-TURN-7: a pass ending in Failed is logged, the tail stays unprocessed."""
    app, agent, _ = rig
    await app.on_message(user_message(text="4500 такси"))
    agent.turn_env = turn_env(tmp_path, turn="limit.jsonl", exit=1)
    await agent.run_pass(7)
    assert app.bot.texts() == [] and len(app.log_for(7).unprocessed({})) == 1


async def test_usage_limit_on_a_question_names_the_reset_time_in_russian(rig, tmp_path):
    """REQ-TURN-6: the presenter's Russian Texts and the person's tz word the limit failure."""
    app, agent, _ = rig
    agent.turn_env = turn_env(tmp_path, turn="limit.jsonl", exit=1)
    await app.on_message(user_message(text="сколько?"))
    await app.drain()
    assert any("Лимит" in t for t in app.bot.texts())


async def test_pass_and_question_of_one_person_never_overlap(rig, tmp_path):
    """REQ-TURN-5: one lane per person — a question waits while that person's pass runs; another person's does not."""
    app, agent, _ = rig
    agent.turn_env = turn_env(tmp_path, mode="hang")
    agent.start_pass(7)
    await asyncio.sleep(0.5)
    assert len(records(tmp_path)) == 1  # the pass is running
    agent.turn_env = turn_env(tmp_path)
    await app.on_message(user_message(text="сколько?"))
    await app.on_message(user_message(uid=8, text="сколько?"))
    await asyncio.sleep(0.8)
    recs = records(tmp_path)
    assert len(recs) == 2 and recs[1]["cwd"] == str(app.layout.user_dir(8))  # 8 ran, 7's question waits
    agent.passes[7].cancel()
    await app.drain()
    assert len(records(tmp_path)) == 3


async def test_start_pass_is_idempotent_while_pending(rig, tmp_path):
    """REQ-SCHED-2: at most one pending pass per person."""
    _, agent, _ = rig
    agent.turn_env = turn_env(tmp_path, mode="hang")
    assert agent.start_pass(7) is True and agent.start_pass(7) is False
    agent.passes[7].cancel()
    await asyncio.gather(agent.passes[7], return_exceptions=True)
    assert 7 not in agent.passes


async def test_recover_forgets_stranded_turns(tmp_path, fake_path):
    """REQ-TURN-9: a marker left by a crashed process is logged and forgotten, and its agent killed."""
    cfg = config(tmp_path)
    state = tmp_path / "state"
    script = textwrap.dedent(f"""
        import asyncio, os
        from televibe import Account, ClaudeCode, Engine, Started
        async def main():
            async with Engine({str(state)!r}) as engine:
                claude = ClaudeCode(Account({str(cfg.account_home)!r}))
                async with engine.turn("hang", session=claude.new_session({str(tmp_path)!r}),
                                       env={turn_env(tmp_path, mode="hang")!r}) as turn:
                    async for event in turn:
                        if isinstance(event, Started):
                            os._exit(0)
        asyncio.run(main())
    """)
    subprocess.run(
        [sys.executable, "-c", script], check=True, env={**os.environ, "PATH": os.environ["PATH"]}, timeout=30
    )
    assert list((state / "turns").glob("*.json"))
    async with Engine(state) as engine:
        app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir))
        agent = Agent(config=cfg, app=app, engine=engine, provider=make_provider(cfg))
        assert await agent.recover() == 1
        assert list((state / "turns").glob("*.json")) == []
        app.close()
