"""Reminders: the agent's dated obligations, sent once a day by Python (SPEC REQ-REMIND-1..4)."""

import sqlite3
from datetime import date

import pytest
from tgkit import FakeBot

from telegrind.agent import Agent
from telegrind.app import App
from telegrind.config import Config
from telegrind.facts import Facts, FactsUnavailable, Reminder
from telegrind.layout import Layout
from telegrind.reminders import FOOTER, HEADER, digest, label, next_date
from telegrind.scheduler import Scheduler

TODAY = date(2026, 10, 21)
TABLE = (
    "CREATE TABLE reminders(entry_id INTEGER NOT NULL, what TEXT NOT NULL, due TEXT, monthly_day INTEGER,"
    " done INTEGER NOT NULL DEFAULT 0);"
)


def once(due, what="Арман вернёт 15 000 ₸"):
    return Reminder(1, what, due, None)


def monthly(day, what="рассрочка Eurodent 341 251 ₸"):
    return Reminder(2, what, None, day)


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        (once("2026-10-22"), "завтра"),
        (once("2026-10-21"), "сегодня"),
        (once("2026-10-23"), None),
        (once("2026-10-18"), "просрочено на 3 дн."),
        (once("2026-10-14"), "просрочено на 7 дн."),
        (once("2026-10-13"), None),
        (once("до 20 ноября"), None),
        (monthly(22), "завтра"),
        (monthly(21), "сегодня"),
        (monthly(20), None),  # a monthly payment is never overdue: next month's 20th is a month away
        (monthly(0), None),
    ],
)
def test_label(r, expected):
    """REQ-REMIND-2: tomorrow, today, a one-off overdue up to 7 days; a monthly day is never overdue."""
    assert label(r, TODAY) == expected


def test_a_monthly_day_past_the_month_end_falls_on_its_last_day():
    """REQ-REMIND-1: the 31st in November is the 30th; the day before the 1st is the month's last day."""
    assert next_date(monthly(31), date(2026, 11, 29)) == date(2026, 11, 30)
    assert label(monthly(1), date(2026, 10, 31)) == "завтра"
    assert label(monthly(31), date(2026, 2, 27)) == "завтра"


def test_digest_lists_overdue_first_and_says_nothing_when_nothing_is_due():
    """REQ-REMIND-2, REQ-REMIND-4: one message, oldest date first, closing with how to close a reminder."""
    text = digest([monthly(22), once("2026-10-19"), once("2026-12-01", "не сегодня")], TODAY)
    assert text == "\n".join(
        [HEADER, "• просрочено на 2 дн. — Арман вернёт 15 000 ₸", "• завтра — рассрочка Eurodent 341 251 ₸", "", FOOTER]
    )
    assert digest([once("2026-12-01")], TODAY) is None


def test_facts_reads_only_open_reminders(tmp_path):
    """REQ-REMIND-1, REQ-FACTS-2: rows with done = 1 are closed; no table yet means none."""
    path = tmp_path / "facts.sqlite"
    assert Facts(path).reminders() == []
    with sqlite3.connect(path) as conn:
        conn.executescript(
            TABLE + "INSERT INTO reminders VALUES (5, 'a', '2026-10-22', NULL, 0), (6, 'b', NULL, 22, 0),"
            " (7, 'c', '2026-10-22', NULL, 1);"
        )
    assert sorted(Facts(path).reminders(), key=lambda r: r.entry_id) == [
        Reminder(5, "a", "2026-10-22", None),
        Reminder(6, "b", None, 22),
    ]


@pytest.fixture
def rig(tmp_path):
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})
    clock = {"now": 0}
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: clock["now"])
    agent = Agent(config=cfg, app=app, engine=None, provider=None)
    app.log_for(7)
    with sqlite3.connect(app.layout.facts_path(7)) as conn:
        conn.executescript(TABLE + "INSERT INTO reminders VALUES (5, 'Арман вернёт 15 000 ₸', '2026-10-22', NULL, 0);")

    async def sleep(_):
        pass

    sched = Scheduler(app, agent, sleep=sleep)
    yield app, agent, sched, clock
    app.close()


def at(clock, agent, day, hour):
    local = agent.local_now(7).replace(year=2026, month=10, day=day, hour=hour, minute=5, second=0, microsecond=0)
    clock["now"] = int(local.timestamp())


async def test_sent_once_a_day_at_or_after_the_remind_hour(rig):
    """REQ-REMIND-2, REQ-REMIND-3: nothing before 10:00; one message after it, through say(), and not again that
    day; a bot entry like any other."""
    app, agent, sched, clock = rig
    at(clock, agent, 21, 9)
    await sched.tick()
    assert app.bot.texts() == []
    at(clock, agent, 21, 15)  # the bot was down at 10:00: the day is not lost
    await sched.tick()
    await sched.tick()
    texts = app.bot.texts()
    assert len(texts) == 1 and "• завтра — Арман вернёт 15 000 ₸" in texts[0]
    _, kw = app.bot.sends()[0]
    assert kw["parse_mode"] is None
    assert app.log_for(7).reminded_on("2026-10-21")
    at(clock, agent, 22, 10)
    await sched.tick()
    assert len(app.bot.texts()) == 2 and "• сегодня —" in app.bot.texts()[1]


async def test_a_failed_send_is_retried_on_the_next_tick(rig):
    """REQ-REMIND-3: the day is recorded only once the message is out."""
    app, agent, sched, clock = rig
    app.bot.fail["send_message"] = 1
    at(clock, agent, 21, 10)
    await sched.tick()
    assert not app.log_for(7).reminded_on("2026-10-21")
    await sched.tick()
    store = app.log_for(7)
    assert store.reminded_on("2026-10-21") and len(app.bot.texts()) == 2  # the failed call, then the one out
    assert store.conn.execute("SELECT count(*) FROM entries WHERE author = 'bot'").fetchone()[0] == 1
    await sched.tick()
    assert len(app.bot.texts()) == 2


async def test_unreadable_facts_skip_the_tick_and_the_pass_check_still_runs(rig, monkeypatch, caplog):
    """REQ-REMIND-3: an unreadable facts.sqlite records nothing; the next tick asks again."""
    app, agent, sched, clock = rig

    def broken(self):
        raise FactsUnavailable("OperationalError")

    monkeypatch.setattr(Facts, "reminders", broken)
    at(clock, agent, 21, 10)
    await sched.tick()
    assert app.bot.texts() == [] and not app.log_for(7).reminded_on("2026-10-21")
    assert "reminders skipped" in caplog.text
