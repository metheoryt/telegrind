"""Reminders: dates the agent keeps in facts.sqlite, sent once a day without a model call (SPEC REQ-REMIND-1..4)."""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from telegrind.facts import FactsUnavailable, Reminder

if TYPE_CHECKING:
    from telegrind.agent import Agent
    from telegrind.app import App

log = logging.getLogger(__name__)

OVERDUE_DAYS = 7  # a one-off past its date is repeated this many days, then left to the person
HEADER = "Напоминания:"
FOOTER = "Когда оплатишь или тебе вернут — напиши об этом обычным сообщением, и напоминание закроется."


def _on_day(year: int, month: int, day: int) -> date:
    """A monthly day past the month's end falls on its last day: the 31st is 30 November."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_date(r: Reminder, today: date) -> date | None:
    """The date that matters today: a one-off's own date; a monthly payment's this month, or next once it passed."""
    if r.due is not None:
        try:
            return date.fromisoformat(r.due)
        except ValueError:
            return None
    if r.monthly_day is None or not 1 <= r.monthly_day <= 31:
        return None
    this = _on_day(today.year, today.month, r.monthly_day)
    if this >= today:
        return this
    nxt = today.replace(day=1) + timedelta(days=32)
    return _on_day(nxt.year, nxt.month, r.monthly_day)


def label(r: Reminder, today: date) -> str | None:
    """REQ-REMIND-2: tomorrow, today, or a one-off overdue up to OVERDUE_DAYS; anything else says nothing today."""
    when = next_date(r, today)
    if when is None:
        return None
    days = (when - today).days
    if days == 1:
        return "завтра"
    if days == 0:
        return "сегодня"
    if r.due is not None and -OVERDUE_DAYS <= days < 0:
        return f"просрочено на {-days} дн."
    return None


def digest(reminders: list[Reminder], today: date) -> str | None:
    """One message for the day, overdue first, or None when nothing is due."""
    items = [(next_date(r, today), lab, r.what) for r in reminders if (lab := label(r, today)) is not None]
    if not items:
        return None
    lines = [f"• {lab} — {what}" for _, lab, what in sorted(items, key=lambda i: (i[0], i[2]))]
    return "\n".join([HEADER, *lines, "", FOOTER])


class Reminders:
    def __init__(self, app: App, agent: Agent) -> None:
        self.app, self.agent = app, agent

    async def tick(self, uid: int) -> bool:
        """REQ-REMIND-2, REQ-REMIND-3: at or past the remind hour, once per local day, recorded only once sent."""
        local = self.agent.local_now(uid)
        if local.hour < self.app.config.remind_hour:
            return False
        day = local.date().isoformat()
        store = self.app.log_for(uid)
        if store.reminded_on(day):
            return False
        try:
            text = digest(self.agent.facts(uid).reminders(), local.date())
        except FactsUnavailable as exc:  # unknown is not empty: the next tick asks again
            log.warning("user %d: facts.sqlite unavailable (%s); reminders skipped", uid, exc.reason)
            return False
        if text is None:
            return False
        if await self.app.say(uid, text, None) is None:
            return False  # the next tick tries again
        store.mark_reminded(day, self.app.clock())
        return True
