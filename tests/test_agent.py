"""Agent turns through televibe against the fake claude (SPEC section 7, REQ-TEST-3)."""

import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta

import pytest
from conftest import records, turn_env
from televibe import Engine
from televibe.telegram import Presenter
from tgkit import NOW, FakeBot, bot_message, reaction, user_message

from telegrind import agent as agent_module
from telegrind import editor
from telegrind.agent import (
    ASIDE_EVERY,
    ASIDE_TEXT,
    LONG_TURN_TEXT,
    NO_AGENT_TEXT,
    QUOTA_TEXT,
    Agent,
    _Watched,
    make_provider,
)
from telegrind.app import App
from telegrind.config import Config
from telegrind.facts import Facts, FactsUnavailable
from telegrind.layout import Layout
from telegrind.log import Log
from telegrind.media import MediaJobs

ANSWER = "televibe fixture"  # the `result` text of tests/fixtures/claude/turn.jsonl


def config(tmp_path, **env):
    return Config.from_env(
        {
            "BOT_TOKEN": "t",
            "ALLOWED_USER_IDS": "7,8",
            "DATA_DIR": str(tmp_path / "data"),
            "ACCOUNT_HOME": str(tmp_path / "acc"),
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            "EDIT_ANSWERS": "0",  # the editor's own tests turn it on (REQ-TURN-12)
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
    await app.on_message(user_message(text="/q сколько такси?"))
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
    assert rec["cwd"] == str(app.layout.user_dir(7)) and rec["prompt"] == "/q сколько такси?"
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
    await app.on_message(user_message(text="/q сколько такси?"))
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
    prompt = second["argv"][second["argv"].index("--append-system-prompt") + 1]
    assert ANSWER in prompt and "Человек отвечает на это сообщение бота:" in prompt


def system_prompt(rec):
    return rec["argv"][rec["argv"].index("--append-system-prompt") + 1]


@pytest.mark.parametrize(
    ("first", "wording"),
    [
        ({}, "Человек отвечает на свою запись:"),
        ({"forward": ("user", "Иван Петров")}, "Человек отвечает на пересланное сообщение:"),
    ],
    ids=["own", "forward"],
)
async def test_reply_quote_is_worded_by_who_wrote_the_replied_entry(rig, tmp_path, first, wording):
    """REQ-TURN-2: the quoted text is introduced as the person's own entry, or a forward — not as the bot's."""
    app, _, _ = rig
    replied = user_message(text="перевёл 5000", **first)
    await app.on_message(replied)
    await app.on_message(user_message(text="/q это много?", message_id=11, reply_to=replied))
    await app.drain()
    prompt = system_prompt(records(tmp_path)[0])
    assert wording in prompt and "перевёл 5000" in prompt and "сообщение бота" not in prompt


async def test_new_question_is_a_new_session(rig, tmp_path):
    """REQ-TURN-3: a question that replies to nothing starts a new chain and a new session."""
    app, *_ = rig
    await app.on_message(user_message(text="/q сколько такси?"))
    await app.on_message(user_message(text="/q а сколько еды?", message_id=11))
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
    await app.on_message(user_message(text="/q сколько?"))
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
    await app.on_message(user_message(text="/q раз?"))
    await app.drain()
    await app.on_message(user_message(text="/q два?", message_id=11))
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


@pytest.mark.parametrize("failing", ["every", "at_start", "after"])
async def test_pass_with_unreadable_facts_posts_nothing(rig, tmp_path, monkeypatch, caplog, failing):
    """REQ-TURN-7 without a reading: a pass whose before or after mark cannot be read posts no digest and logs.

    `at_start` fails only the Started mark: falling back to the entry mark would see a change and post."""
    app, agent, _ = rig
    await app.on_message(user_message(text="4500 такси"))
    agent.turn_env = turn_env(tmp_path, mode="pass", sleep=0.2)  # the pass does change processed
    real_mark, calls = Facts.mark, []

    def mark(self):
        calls.append(1)
        n = len(calls)
        if failing == "every" or (failing == "at_start" and n == 2) or (failing == "after" and n == 3):
            raise FactsUnavailable("OperationalError")
        return real_mark(self)

    monkeypatch.setattr(Facts, "mark", mark)
    await agent.run_pass(7)
    assert len(calls) == 3  # entry, Started, after: the turn ran
    assert app.bot.texts() == []
    assert any("7" in r.getMessage() and "OperationalError" in r.getMessage() for r in caplog.records)


async def test_unreadable_tz_falls_back_to_the_default_zone(rig, monkeypatch, caplog):
    """settings.tz that cannot be read now is the default zone, with a log line."""
    _, agent, _ = rig

    def tz(self):
        raise FactsUnavailable("OperationalError")

    monkeypatch.setattr(Facts, "tz", tz)
    assert agent.tz_of(7).key == agent.config.default_tz
    assert any("7" in r.getMessage() and "OperationalError" in r.getMessage() for r in caplog.records)


class GatedBot(FakeBot):
    """A FakeBot whose download_file waits for `gate`: a slow getFile."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def download_file(self, file_path, destination=None, **kwargs):
        await self.gate.wait()
        return await super().download_file(file_path, destination, **kwargs)


def snapshot_media_at_turn(monkeypatch, app):
    """What the entry's media_sha256 was when the question's turn was booked (turn_add runs just before it)."""
    seen = []
    real = Log.turn_add

    def turn_add(self, kind, at):
        if kind == "question":
            seen.append(app.log_for(7).entry(10).media_sha256)
        return real(self, kind, at)

    monkeypatch.setattr(Log, "turn_add", turn_add)
    return seen


async def test_a_question_on_a_photo_waits_for_its_download(rig, tmp_path, monkeypatch):
    """REQ-MSG-2, REQ-TURN-1: a photo addressed with ❤: the turn starts after the file is stored."""
    app, _, _ = rig
    app.bot, app.media = GatedBot(), MediaJobs(app)
    seen = snapshot_media_at_turn(monkeypatch, app)
    await app.on_message(user_message(photo=True, caption="сколько тут?"))
    await app.on_reaction(reaction(emoji="❤"))
    try:
        await asyncio.sleep(0.3)  # the download hangs; the answer must not have started its turn
        assert seen == [] and records(tmp_path) == []
    finally:
        app.bot.gate.set()  # or the rig's drain waits on the download forever
    await app.drain()
    stored = app.log_for(7).entry(10).media_sha256
    assert stored is not None and seen == [stored]
    assert len(records(tmp_path)) == 1 and ANSWER in app.bot.texts()[-1]


async def test_a_question_on_a_photo_answers_without_it_after_the_wait(rig, tmp_path, monkeypatch, caplog):
    """The wait is bounded: past MEDIA_WAIT_S the turn runs without the file, and says so in the log."""
    app, _, _ = rig
    app.bot, app.media = GatedBot(), MediaJobs(app)
    monkeypatch.setattr(agent_module, "MEDIA_WAIT_S", 0.2)
    seen = snapshot_media_at_turn(monkeypatch, app)
    await app.on_message(user_message(photo=True, caption="сколько тут?"))
    await app.on_reaction(reaction(emoji="❤"))
    try:
        for _ in range(100):
            if records(tmp_path) and app.bot.texts():
                break
            await asyncio.sleep(0.05)
        assert seen == [None] and ANSWER in app.bot.texts()[-1]
        assert any("10" in r.getMessage() and "without" in r.getMessage() for r in caplog.records)
    finally:
        app.bot.gate.set()
    await app.drain()
    assert app.log_for(7).entry(10).media_sha256 is not None  # the download itself was not cancelled


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
    await app.on_message(user_message(text="/q сколько такси?", message_id=11))
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
    await app.on_message(user_message(text="/q сколько?"))
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
    await app.on_message(user_message(text="/q сколько?"))
    await app.on_message(user_message(uid=8, text="/q сколько?"))
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


def at(minutes: int) -> datetime:
    return NOW - timedelta(hours=1) + timedelta(minutes=minutes)


async def test_an_addressed_turn_carries_the_context_before_it(rig, tmp_path):
    """REQ-TRIGGER-4, REQ-TRIGGER-5: ❤ → a turn with the entry's text; the instructions say so, date the message
    and list the entries before it with time and author, a deleted one marked; nothing after it."""
    app, _, _ = rig
    await app.on_message(user_message(text="кофе 1500", message_id=1, date=at(0)))
    await app.on_message(user_message(text="курс 520", message_id=2, date=at(1), forward=("user", "Иван")))
    await app.on_message(user_message(text="такси 500", message_id=3, date=at(2)))
    await app.on_reaction(reaction(message_id=3, emoji="💔"))
    await app.on_message(user_message(text="вчера было не 500, а 5000", message_id=4, date=at(3)))
    await app.on_message(user_message(text="потом", message_id=5, date=at(4)))
    await app.on_reaction(reaction(message_id=4, emoji="❤"))
    await app.drain()
    rec = records(tmp_path)[0]
    assert rec["prompt"] == "вчера было не 500, а 5000"
    prompt = system_prompt(rec)
    assert "❤" in prompt and "2026-10-15" in prompt
    lines = prompt[prompt.index("<<<") :]
    assert lines.index("курс 520") < lines.index("кофе 1500") < lines.index("такси 500")  # a forward is dated by origin
    assert "forward: Иван" in lines and "(удалено)" in lines and "потом" not in lines
    assert "вчера было не 500" not in lines  # the message itself is the prompt, not its own context


async def test_an_addressed_reply_takes_its_context_around_the_replied_entry(rig, tmp_path):
    """REQ-TRIGGER-5: ❤ on a reply → the context is the entries before the replied one, plus the replied text."""
    app, _, _ = rig
    await app.on_message(user_message(text="старое до", message_id=1, date=at(0)))
    replied = user_message(text="такси 500", message_id=2, date=at(1))
    await app.on_message(replied)
    await app.on_message(user_message(text="новое", message_id=3, date=at(2)))
    await app.on_message(user_message(text="это было 5000", message_id=4, date=at(3), reply_to=replied))
    await app.on_reaction(reaction(message_id=4, emoji="❤"))
    await app.drain()
    prompt = system_prompt(records(tmp_path)[0])
    assert "Человек отвечает на свою запись:" in prompt and "такси 500" in prompt
    assert "старое до" in prompt and "новое" not in prompt


async def test_an_addressed_reply_to_an_answer_continues_its_chain(rig, tmp_path):
    """REQ-TRIGGER-5, REQ-TURN-3: ❤ on a message replying to the bot's answer resumes that answer's session."""
    app, _, _ = rig
    await app.on_message(user_message(text="/q сколько такси"))
    await app.drain()
    sent_id = app.log_for(7).conn.execute("SELECT message_id FROM entries WHERE author = 'bot'").fetchone()[0]
    answer = bot_message(message_id=sent_id, text=ANSWER)
    # A reply to the bot is a question at once (REQ-TRIGGER-1); ❤ on it asks again in the same chain.
    await app.on_message(user_message(text="а за сентябрь", message_id=11, reply_to=answer))
    await app.drain()
    await app.on_reaction(reaction(message_id=11, emoji="❤"))
    await app.drain()
    first, _, third = records(tmp_path)
    assert first["argv"][first["argv"].index("--session-id") + 1] == third["argv"][third["argv"].index("--resume") + 1]


async def test_an_addressed_turn_leaves_no_receipt(rig, tmp_path):
    """REQ-RECEIPT-4: ❤ takes the entry out of the log, so 💔 goes and does not come back after the answer."""
    app, _, _ = rig
    await app.on_message(user_message(text="вчера было 5000"))
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    assert [r[1] for r in app.bot.reactions()] == ["💔", None, "👀", "👨‍💻", None]
    assert app.log_for(7).entry(10).receipt is None and ANSWER in app.bot.texts()[-1]


async def test_every_fifth_question_may_ask_an_aside(rig, tmp_path):
    """REQ-CONTRACT-17: every ASIDE_EVERY-th question turn's instructions allow one short question by the way."""
    app, _, _ = rig
    for i in range(ASIDE_EVERY):
        await app.on_message(user_message(text=f"/start {i}", message_id=10 + i))
        await app.drain()
    asides = [ASIDE_TEXT in system_prompt(r) for r in records(tmp_path)]
    assert asides == [False] * (ASIDE_EVERY - 1) + [True]


async def test_a_long_question_says_so_and_answers_when_done(rig, tmp_path, monkeypatch):
    """REQ-TURN-11: a question still running after LONG_TURN_S gets the notice in its draft, not a second message;
    the answer comes when it ends."""
    app, agent, _ = rig
    notes = note_spy(monkeypatch)
    monkeypatch.setattr(agent_module, "LONG_TURN_S", 0.2)
    agent.turn_env = turn_env(tmp_path, sleep=0.8)
    await app.on_message(user_message(text="/q разбери выписки"))
    await app.drain()
    assert notes == [LONG_TURN_TEXT]
    assert app.bot.texts() == [ANSWER]


def note_spy(monkeypatch) -> list[str | None]:
    """Every Presenter.note text, in order; the draft it shows in is televibe's to test."""
    notes: list[str | None] = []
    real = Presenter.note

    def note(self, text):
        notes.append(text)
        return real(self, text)

    monkeypatch.setattr(Presenter, "note", note)
    return notes


async def test_a_quick_question_says_nothing_about_time(rig, tmp_path, monkeypatch):
    """REQ-TURN-11: a question that ends before LONG_TURN_S gets its answer alone, and no notice after it."""
    app, _, _ = rig
    notes = note_spy(monkeypatch)
    monkeypatch.setattr(agent_module, "LONG_TURN_S", 0.3)
    await app.on_message(user_message(text="/q сколько такси?"))
    await app.drain()
    await asyncio.sleep(0.5)
    assert app.bot.texts() == [ANSWER] and notes == []


EDITED = "В сентябре на такси ушло 12 400 ₸, 9 поездок."  # structured_output of tests/fixtures/claude/ask_edit.jsonl


async def test_the_answer_is_edited_before_it_is_sent(rig, tmp_path):
    """REQ-TURN-12: the full answer goes through the one-shot editor with the question; the edited text is sent and
    stored, the full one kept as a `drafted` event."""
    app, agent, _ = rig
    agent.config = config(tmp_path, EDIT_ANSWERS="1")
    agent.turn_env = turn_env(tmp_path, ask="ask_edit.jsonl")
    await app.on_message(user_message(text="/q сколько такси?"))
    await app.drain()
    assert app.bot.texts() == [EDITED]
    store = app.log_for(7)
    sent_id = store.conn.execute("SELECT message_id FROM entries WHERE author = 'bot'").fetchone()[0]
    assert store.entry(sent_id).text == EDITED
    drafted = [e.payload for e in store.events(sent_id) if e.kind == "drafted"]
    assert drafted == [{"text": ANSWER}]
    turn, ask = records(tmp_path)
    assert "--json-schema" not in turn["argv"] and "--json-schema" in ask["argv"]
    argv = ask["argv"]
    assert argv[argv.index("--model") + 1] == editor.MODEL and argv[argv.index("--system-prompt") + 1] == editor.SYSTEM
    assert ask["prompt"] == editor.prompt_for(ANSWER, "/q сколько такси?")
    assert "ELEVENLABS_API_KEY" not in ask["env"] and "BOT_TOKEN" not in ask["env"]


async def test_a_failed_edit_sends_the_full_answer(rig, tmp_path, caplog):
    """REQ-TURN-12: an editor answer not in the schema never loses the answer: the agent's own text is sent."""
    app, agent, _ = rig
    agent.config = config(tmp_path, EDIT_ANSWERS="1")
    agent.turn_env = turn_env(tmp_path, ask="turn.jsonl")  # a result with no structured_output
    await app.on_message(user_message(text="/q сколько такси?"))
    await app.drain()
    assert app.bot.texts() == [ANSWER] and "answer editor failed" in caplog.text
    store = app.log_for(7)
    sent_id = store.conn.execute("SELECT message_id FROM entries WHERE author = 'bot'").fetchone()[0]
    assert [e.kind for e in store.events(sent_id)] == ["bot_sent"]


async def test_with_the_editor_the_draft_never_shows_the_agents_text():
    """REQ-TURN-12: the agent's messages are held back from the presenter; Done carries the edited text."""
    from televibe.events import Done
    from televibe.events import Message as AgentMessage

    async def events():
        yield AgentMessage(text="полный ответ")
        yield Done(session=None, text="полный ответ", usage={})

    async def shorten(text):
        return "коротко"

    seen = [e async for e in _Watched(events(), shorten)]
    assert len(seen) == 1 and seen[0].text == "коротко"
    plain = [e async for e in _Watched(events())]
    assert [type(e).__name__ for e in plain] == ["Message", "Done"] and plain[1].text == "полный ответ"
