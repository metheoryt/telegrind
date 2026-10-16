"""The schedule: a quiet-hour pass and a backlog pass (SPEC section 9)."""

import sqlite3

import pytest
from tgkit import FakeBot, user_message

from telegrind.agent import Agent
from telegrind.app import App
from telegrind.config import Config
from telegrind.layout import Layout
from telegrind.scheduler import Scheduler


class FakeAgent(Agent):
    """start_pass records instead of running."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.started = []

    def start_pass(self, uid):
        if uid in self.started:
            return False
        self.started.append(uid)
        return True


@pytest.fixture
def rig(tmp_path):
    cfg = Config.from_env(
        {
            "BOT_TOKEN": "t",
            "ALLOWED_USER_IDS": "7,8",
            "DATA_DIR": str(tmp_path / "data"),
            "BACKLOG_LIMIT": "2",
            "QUIET_HOUR": "4",
        }
    )
    clock = {"now": 1_760_000_000}  # 2026-10-09 ~07:33 UTC → 13:33 Almaty
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: clock["now"])
    agent = FakeAgent(config=cfg, app=app, engine=None, provider=None)
    slept = []

    async def sleep(s):
        slept.append(s)

    sched = Scheduler(app, agent, sleep=sleep)
    app.watcher = sched
    yield app, agent, sched, clock, slept
    app.close()


def at_local_hour(clock, agent, uid, hour):
    local = agent.local_now(uid).replace(hour=hour, minute=5, second=0, microsecond=0)
    clock["now"] = int(local.timestamp())


async def test_backlog_pass_starts_over_the_limit(rig):
    """REQ-SCHED-2: more than BACKLOG_LIMIT unprocessed entries → a pass, once."""
    app, agent, *_ = rig
    for i in range(2):  # exactly BACKLOG_LIMIT: not over it
        await app.on_message(user_message(text=f"{i} x", message_id=10 + i))
    assert agent.started == []
    await app.on_message(user_message(text="2 x", message_id=12))
    assert agent.started == [7]  # started on the third entry, not again on later ones
    await app.on_message(user_message(text="4 x", message_id=14))
    assert agent.started == [7]


async def test_backlog_pass_is_not_queued_twice(rig):
    """REQ-SCHED-2: at most one pending pass per person, even when both triggers fire."""
    app, agent, sched, clock, _ = rig
    for i in range(3):
        await app.on_message(user_message(text=f"{i} x", message_id=10 + i))
    at_local_hour(clock, agent, 7, 4)
    assert await sched.tick() == [] and agent.started == [7]


async def test_quiet_hour_pass_once_a_day_only_with_work(rig):
    """REQ-SCHED-1, REQ-SCHED-3: at the quiet hour, a pass if the tail is non-empty; not twice; from the tables."""
    app, agent, sched, clock, slept = rig
    await app.on_message(user_message(text="x"))
    await app.on_message(user_message(uid=8, text="y"))
    at_local_hour(clock, agent, 7, 13)
    assert await sched.tick() == []
    at_local_hour(clock, agent, 7, 4)
    assert await sched.tick() == [7, 8] and slept == [5.0]  # spread between people
    app.log_for(7).turn_add("pass", app.clock())
    app.log_for(8).turn_add("pass", app.clock())  # what run_pass records
    agent.started.clear()
    assert await sched.tick() == []  # already passed today, per the ledger — a restart changes nothing
    # a person whose tail is processed is not disturbed
    conn = sqlite3.connect(app.layout.facts_path(8))
    conn.execute("CREATE TABLE processed(entry_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, at INTEGER NOT NULL)")
    conn.execute("INSERT INTO processed VALUES (10, 1, 1)")
    conn.commit()
    conn.close()
    clock["now"] += 86400
    assert await sched.tick() == [7]


async def test_quiet_hour_in_the_persons_zone(rig):
    """REQ-SCHED-1: settings.tz moves the quiet hour to the person's clock."""
    app, agent, sched, clock, _ = rig
    await app.on_message(user_message(text="x"))
    conn = sqlite3.connect(app.layout.facts_path(7))
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO settings VALUES ('tz', 'Asia/Bangkok')")
    conn.commit()
    conn.close()
    at_local_hour(clock, agent, 7, 4)  # 04:05 Bangkok == 05:05 Almaty
    assert await sched.tick() == [7]


async def test_one_failing_user_does_not_stop_the_tick(rig, monkeypatch):
    """A user whose due() raises is logged and skipped; the next user still starts."""
    app, agent, sched, clock, _ = rig
    await app.on_message(user_message(text="x"))
    await app.on_message(user_message(uid=8, text="y"))
    at_local_hour(clock, agent, 7, 4)
    real_due = sched.due

    def due(uid):
        if uid == 7:
            raise sqlite3.DatabaseError("locked")
        return real_due(uid)

    monkeypatch.setattr(sched, "due", due)
    assert await sched.tick() == [8]
