"""Reminders the person asked for: kept by the agent, sent by Python on time (SPEC REQ-REMIND-1..4)."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from tgkit import FakeBot

from telegrind.agent import Agent
from telegrind.app import App
from telegrind.config import Config
from telegrind.facts import Facts, FactsUnavailable, Reminder
from telegrind.layout import Layout
from telegrind.reminders import PREFIX, due_now, occurrence, text_of
from telegrind.scheduler import Scheduler
from telegrind.triggers import question_text

TZ = ZoneInfo("Asia/Almaty")
TABLE = (
    "CREATE TABLE reminders(entry_id INTEGER NOT NULL, what TEXT NOT NULL, due TEXT, monthly_day INTEGER,"
    " time TEXT, done INTEGER NOT NULL DEFAULT 0, proposed INTEGER NOT NULL DEFAULT 0);"
)


def now(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


def once(due, at=None, what="позвонить Арману"):
    return Reminder(1, what, due, None, at)


def monthly(day, at=None, what="рассрочка Eurodent 341 251 ₸"):
    return Reminder(2, what, None, day, at)


@pytest.mark.parametrize(
    ("r", "at", "fires"),
    [
        (once("2026-10-21"), now(21, 9, 59), False),  # no time named: the remind hour, 10:00
        (once("2026-10-21"), now(21, 10), True),
        (once("2026-10-21", "15:30"), now(21, 15, 29), False),
        (once("2026-10-21", "15:30"), now(21, 15, 30), True),
        (once("2026-10-21", "23:00"), now(22, 9), True),  # missed while the bot was down: still goes out
        (once("2026-10-19"), now(21, 10), False),  # older than a day: not dug up
        (once("2026-10-22"), now(21, 23), False),
        (once("до 20 ноября"), now(21, 10), False),
        (once("2026-10-21", "в обед"), now(21, 10), True),  # a bad time falls back to the remind hour
        (monthly(22), now(22, 10), True),
        (monthly(22), now(23, 10), False),  # a monthly day is never late
        (monthly(22), now(21, 10), False),
        (monthly(0), now(21, 10), False),
    ],
)
def test_due_now(r, at, fires):
    """REQ-REMIND-2: a row fires on its day at its time (or the remind hour); a one-off up to a day late."""
    assert (due_now(r, at, 10) is not None) is fires


def test_a_monthly_day_past_the_month_end_falls_on_its_last_day():
    """REQ-REMIND-1: the 31st in November is the 30th."""
    assert occurrence(monthly(31), now(5, 10, month=11).date()) == now(30, 0, month=11).date()


def test_the_text_is_the_persons_words_and_a_late_one_says_when():
    """REQ-REMIND-2: «⏰ what»; one more than an hour late names the time it was for."""
    r = once("2026-10-21", "23:00")
    assert text_of(r, now(21, 23), now(21, 23, 1)) == PREFIX + "позвонить Арману"
    assert text_of(r, now(21, 23), now(22, 9)) == PREFIX + "позвонить Арману\n(было на 21.10 23:00)"


def test_facts_reads_only_open_reminders(tmp_path):
    """REQ-REMIND-1, REQ-REMIND-5, REQ-FACTS-2: a closed row (done = 1) and an offered one the person has not
    accepted (proposed = 1) are not read; no table yet means none."""
    path = tmp_path / "facts.sqlite"
    assert Facts(path).reminders() == []
    with sqlite3.connect(path) as conn:
        conn.executescript(
            TABLE + "INSERT INTO reminders VALUES (5, 'a', '2026-10-22', NULL, '15:00', 0, 0),"
            " (6, 'b', NULL, 22, NULL, 0, 0), (7, 'c', '2026-10-22', NULL, NULL, 1, 0),"
            " (8, 'd', '2026-10-22', NULL, NULL, 0, 1);"
        )
    assert sorted(Facts(path).reminders(), key=lambda r: r.entry_id) == [
        Reminder(5, "a", "2026-10-22", None, "15:00"),
        Reminder(6, "b", None, 22, None),
    ]


@pytest.fixture
def rig(tmp_path):
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})
    clock = {"now": 0}
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: clock["now"])
    agent = Agent(config=cfg, app=app, engine=None, provider=None)
    app.log_for(7)
    with sqlite3.connect(app.layout.facts_path(7)) as conn:
        conn.executescript(
            TABLE + "INSERT INTO reminders VALUES (12, 'позвонить Арману', '2026-10-21', NULL, '15:30', 0, 0),"
            " (13, 'рассрочка Eurodent', NULL, 21, NULL, 0, 0), (14, 'только предложено', NULL, 21, NULL, 0, 1);"
        )

    async def sleep(_):
        pass

    yield app, Scheduler(app, agent, sleep=sleep), clock
    app.close()


def at(clock, day, hour, minute=0):
    clock["now"] = int(now(day, hour, minute).timestamp())


async def test_each_reminder_goes_out_once_at_its_time(rig):
    """REQ-REMIND-2, REQ-REMIND-3: nothing early; each row once, through say(), replying to the entry that asked."""
    app, sched, clock = rig
    at(clock, 21, 9)
    await sched.tick()
    assert app.bot.texts() == []
    at(clock, 21, 10)
    await sched.tick()
    await sched.tick()
    assert app.bot.texts() == [PREFIX + "рассрочка Eurodent"]
    at(clock, 21, 15, 31)
    await sched.tick()
    assert app.bot.texts()[1:] == [PREFIX + "позвонить Арману"]
    _, kw = app.bot.sends()[1]
    assert kw["parse_mode"] is None and kw["reply_parameters"].message_id == 12
    at(clock, 22, 10)
    await sched.tick()
    assert len(app.bot.texts()) == 2
    assert "только предложено" not in "".join(app.bot.texts())  # REQ-REMIND-5: an offer is never sent


async def test_a_failed_send_is_retried_on_the_next_tick(rig):
    """REQ-REMIND-3: a reminder is recorded as sent only once the message is out."""
    app, sched, clock = rig
    app.bot.fail["send_message"] = 1
    at(clock, 21, 10)
    await sched.tick()
    assert not app.log_for(7).reminder_sent(13, "рассрочка Eurodent", "2026-10-21")
    await sched.tick()
    assert app.log_for(7).reminder_sent(13, "рассрочка Eurodent", "2026-10-21")
    await sched.tick()
    assert app.bot.texts() == [PREFIX + "рассрочка Eurodent"] * 2  # the failed call, then the one out


async def test_unreadable_facts_skip_the_tick(rig, monkeypatch, caplog):
    """REQ-REMIND-3: an unreadable facts.sqlite sends and records nothing; the next tick asks again."""
    app, sched, clock = rig

    def broken(self):
        raise FactsUnavailable("OperationalError")

    monkeypatch.setattr(Facts, "reminders", broken)
    at(clock, 21, 10)
    await sched.tick()
    assert app.bot.texts() == [] and "reminders skipped" in caplog.text


async def test_a_reply_to_a_reminder_is_a_question(rig):
    """REQ-REMIND-4: the reminder is a bot entry, so «отмени» in reply to it reaches the agent."""
    app, sched, clock = rig
    at(clock, 21, 10)
    await sched.tick()
    store = app.log_for(7)
    sent_id = store.conn.execute("SELECT message_id FROM entries WHERE author = 'bot'").fetchone()[0]
    assert question_text("отмени", reply_to_author=store.author_of(sent_id), author="me") == "отмени"
