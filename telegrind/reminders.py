"""Reminders the person asked for: kept by the agent in facts.sqlite, sent by Python on time (SPEC REQ-REMIND-1..4)."""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from telegrind.facts import FactsUnavailable, Reminder

if TYPE_CHECKING:
    from telegrind.agent import Agent
    from telegrind.app import App

log = logging.getLogger(__name__)

LATE_DAYS = 1  # a one-off missed while the bot was down still goes out, marked late, for this long
PREFIX = "⏰ "


def occurrence(r: Reminder, today: date) -> date | None:
    """The date the row fires on near today: a one-off's own date, a monthly day in today's month (the 31st is
    30 November), or None for a row that names neither."""
    if r.due is not None:
        try:
            return date.fromisoformat(r.due)
        except ValueError:
            return None
    if r.monthly_day is None or not 1 <= r.monthly_day <= 31:
        return None
    return today.replace(day=min(r.monthly_day, calendar.monthrange(today.year, today.month)[1]))


def clock_time(r: Reminder, default_hour: int) -> time:
    """The row's HH:MM, or the remind hour when it names none (or names it badly)."""
    if r.time:
        try:
            return time.fromisoformat(r.time)
        except ValueError:
            pass
    return time(default_hour)


def due_now(r: Reminder, now: datetime, default_hour: int) -> tuple[date, datetime] | None:
    """REQ-REMIND-2: (occurrence, fire time) when the row should go out at `now`, else None."""
    day = occurrence(r, now.date())
    if day is None:
        return None
    fire = datetime.combine(day, clock_time(r, default_hour), now.tzinfo)
    if now < fire:
        return None
    if r.due is not None and now.date() - day > timedelta(days=LATE_DAYS):
        return None
    if r.due is None and day != now.date():
        return None
    return day, fire


def text_of(r: Reminder, fire: datetime, now: datetime) -> str:
    """The message: the person's own words; a late one says when it was due."""
    late = now - fire > timedelta(hours=1)
    return PREFIX + r.what + (f"\n(было на {fire:%d.%m %H:%M})" if late else "")


class Reminders:
    def __init__(self, app: App, agent: Agent) -> None:
        self.app, self.agent = app, agent

    async def tick(self, uid: int) -> int:
        """REQ-REMIND-2, REQ-REMIND-3: every row due by now and not yet sent goes out, each once."""
        now = self.agent.local_now(uid)
        try:
            rows = self.agent.facts(uid).reminders()
        except FactsUnavailable as exc:  # unknown is not empty: the next tick asks again
            log.warning("user %d: facts.sqlite unavailable (%s); reminders skipped", uid, exc.reason)
            return 0
        store, sent = self.app.log_for(uid), 0
        for r in rows:
            if (hit := due_now(r, now, self.app.config.remind_hour)) is None:
                continue
            day, fire = hit
            if store.reminder_sent(r.entry_id, r.what, day.isoformat()):
                continue
            if await self.app.say(uid, text_of(r, fire, now), r.entry_id) is None:
                continue  # the next tick tries again
            store.mark_reminder_sent(r.entry_id, r.what, day.isoformat(), self.app.clock())
            sent += 1
        return sent
