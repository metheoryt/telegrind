"""When a pass runs: the quiet hour, and a backlog over the limit (SPEC section 9)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegrind.agent import Agent
from telegrind.app import App

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        app: App,
        agent: Agent,
        *,
        tick_s: float = 60.0,
        spread_s: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.app, self.agent, self.tick_s, self.spread_s, self.sleep = app, agent, tick_s, spread_s, sleep

    def unprocessed_count(self, uid: int) -> int:
        return len(self.app.log_for(uid).unprocessed(self.agent.facts(uid).processed()))

    def due(self, uid: int) -> bool:
        """REQ-SCHED-1, REQ-SCHED-3: the quiet hour, no pass yet today (per the ledger), and something to do."""
        if self.agent.local_now(uid).hour != self.app.config.quiet_hour:
            return False
        if self.app.log_for(uid).turns_since("pass", self.agent.day_start(uid)) > 0:
            return False
        return self.unprocessed_count(uid) > 0

    def on_entry(self, uid: int) -> None:
        """REQ-SCHED-2: a backlog over the limit starts a pass; start_pass keeps it to one pending."""
        if self.unprocessed_count(uid) > self.app.config.backlog_limit and self.agent.start_pass(uid):
            log.info("user %d: backlog pass started", uid)

    async def tick(self) -> list[int]:
        started: list[int] = []
        for uid in self.app.layout.users():
            if uid not in self.app.config.allowed_user_ids or not self.due(uid):
                continue
            if started:
                await self.sleep(self.spread_s)
            if self.agent.start_pass(uid):
                started.append(uid)
        return started

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # the schedule must outlive one bad tick
                log.exception("scheduler tick failed")
            await self.sleep(self.tick_s)
