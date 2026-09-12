"""One turn at a time per session, and the rest queue.

The queue is keyed by the session id, and that one rule covers every case.
Same key, wait; different key, run now — because different fork points are
different sessions, and serialising them would merge two threads the user
deliberately split.

What happens to a follow-up depends only on whether the turn has started.
Not handed over yet: the two go in together as one prompt. Already running:
the follow-up waits, and then runs as the next turn on that session — which
is not the consolation prize, because by then the answer to the first
message exists and the follow-up is answered in light of it.

A third case, and it is not a follow-up: the *same* message handed over
twice while its turn is in flight. `submit` drops it. The hand-off still
succeeds — the message is being handled, just not twice.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegrind.meta import sessions
from telegrind.meta.config import MetaConfig
from telegrind.meta.runtime import TurnResult

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Job:
    chat_id: int
    chat_pk: int
    message_id: int
    text: str
    #: The turn this message continues, as a Telegram message_id.
    key: int


Run = Callable[..., Awaitable[TurnResult]]
Deliver = Callable[[Job, str], Awaitable[None]]
#: `mark(job, True)` when the turn starts, `mark(job, False)` when it failed.
#: A bool rather than an emoji: the receipt vocabulary is the host's, and a
#: second copy of "👀" in here is the kind of duplicate that drifts.
Mark = Callable[[Job, bool], Awaitable[None]]


def _refused_before_the_turn(result: TurnResult) -> bool:
    """Could this failure be a `--resume` naming a session that is not there?

    Only one shape of failure can: the process ran to completion and exited
    with a *positive* code. Measured 2026-09-12, that is exactly what a dead
    resume target does — exit 1, stdout empty, before any model call.

    Everything else is a failure of the turn, not of the resume. A timeout
    or a spawn error has no exit code at all and is not about the session;
    a zero exit means the CLI loaded the session and answered, however
    badly — `is_error`, unparseable JSON, or (since Task 8) an empty result
    are all `ok=False` with the base session demonstrably alive. Retrying
    those cold buys the same outcome for a full cold session, measured at
    $0.21 against 20k cache-creation tokens on 2026-09-12.

    Positive rather than merely non-zero, because `runtime._spawn` returns
    `proc.returncode or 0` and a signal-killed process reports the negative
    of its signal — -9 for SIGKILL, most plausibly the OOM killer. Truthy,
    and about the machine rather than the session: a cold retry there
    doubles the memory pressure that caused it.

    Deliberately not a substring match on the CLI's message: that text is
    truncated to 400 characters and its wording is not a contract, as
    `runtime.run_turn` says in the comment where it is produced.
    """
    return not result.ok and result.exit_code is not None and result.exit_code > 0


class Turns:
    """One worker task per live session key."""

    def __init__(
        self, cfg: MetaConfig, *, run: Run, deliver: Deliver, mark: Mark
    ) -> None:
        self._cfg = cfg
        self._run = run
        self._deliver = deliver
        self._mark = mark
        self._queues: dict[tuple[int, int], asyncio.Queue[Job]] = {}
        self._workers: dict[tuple[int, int], asyncio.Task[None]] = {}
        #: (chat_id, message_id) of every message in a turn that is running
        #: right now. Deliberately *not* «every message ever queued»: a
        #: turn that fails takes its own eyes off, and `receipt_emoji = None`
        #: is exactly what re-opens `record_edited`'s `handed_over` gate —
        #: so that message must be able to come back. Only the overlap is
        #: wrong.
        self._in_flight: set[tuple[int, int]] = set()

    async def submit(self, job: Job) -> None:
        if (job.chat_id, job.message_id) in self._in_flight:
            # The window: `record` commits a bare receipt, and `route`'s
            # question arm does not `claim` until two model calls later.
            # An edit landing in there is handed over a second time, and
            # by then the worker is already inside `_turn` — so this is
            # not a merge, it is a second turn answering the same message
            # on the same session id.
            log.info("message %s is already in a running turn", job.message_id)
            return
        slot = (job.chat_id, job.key)
        queue = self._queues.setdefault(slot, asyncio.Queue())
        await queue.put(job)
        worker = self._workers.get(slot)
        if worker is None or worker.done():
            self._workers[slot] = asyncio.create_task(self._work(slot, queue))

    async def drain(self) -> None:
        """Wait for every worker to finish. Tests only."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()))
            self._workers = {k: t for k, t in self._workers.items() if not t.done()}

    async def _work(self, slot: tuple[int, int], queue: asyncio.Queue[Job]) -> None:
        try:
            await self._loop(slot, queue)
        except Exception:  # a worker that dies silently strands its queue
            log.exception("the worker for %s died", slot)
        finally:
            # A chat runs for months; a dict that only ever grows is a leak
            # with a slow fuse. `submit` recreates both on the next message.
            # Safe without a lock only because nothing between the
            # QueueEmpty that ends `_loop` and this check awaits, so no
            # `submit` can interleave and find a worker that is about to go.
            if queue.empty():
                self._queues.pop(slot, None)
                self._workers.pop(slot, None)

    async def _loop(self, slot: tuple[int, int], queue: asyncio.Queue[Job]) -> None:
        while True:
            try:
                batch = [queue.get_nowait()]
            except asyncio.QueueEmpty:
                return
            # Everything already waiting for this session belongs to the
            # same thought: it arrived before the turn started, so it goes
            # in with it rather than becoming a second turn.
            while True:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            in_flight = {(job.chat_id, job.message_id) for job in batch}
            self._in_flight |= in_flight
            try:
                await self._turn(slot, batch)
            finally:
                self._in_flight -= in_flight

    async def _mark_all(self, batch: list[Job], started: bool) -> None:
        """Move every receipt in the batch, and survive a failure to.

        The receipt is a cue; the turn is the work. A database hiccup or a
        Telegram outage while placing 👀 must not kill the worker, because
        a dead worker is a queue nobody drains.
        """
        for job in batch:
            try:
                await self._mark(job, started)
            except Exception:  # cosmetic, and the row is already safe
                log.exception("could not move the receipt on %s", job.message_id)

    async def _turn(self, slot: tuple[int, int], batch: list[Job]) -> None:
        last = batch[-1]
        await self._mark_all(batch, True)

        chat_id, key = slot
        # The key names the turn this batch continues. When that message is
        # itself in the batch, the conversation is starting and there is
        # nothing to resume; otherwise the key names an earlier turn whose
        # session id we fork off.
        fresh = key in {job.message_id for job in batch}
        resume_from = None if fresh else sessions.session_id(chat_id, key)
        session_id = sessions.session_id(chat_id, last.message_id)

        prompt = "\n".join(job.text for job in batch)
        try:
            result = await self._run(
                prompt, session_id=session_id, resume_from=resume_from
            )
            if resume_from is not None and _refused_before_the_turn(result):
                # The base session may not exist: a merged batch names its
                # session after the last message, so the key's own id was
                # never written — and a transcript can also be pruned, or
                # the warm base rebuilt. Starting fresh loses the thread;
                # a crash string loses the answer. The session chain is
                # self-consistent by design and not unconditionally, so
                # this escape hatch is load-bearing, not belt-and-braces.
                log.info("could not resume %s, starting fresh", resume_from)
                result = await self._run(
                    prompt, session_id=session_id, resume_from=None
                )
        except Exception as exc:  # a worker task that dies takes the queue with it
            log.exception("turn on %s blew up", session_id)
            result = TurnResult(text=f"Что-то сломалось: {exc}", ok=False)

        if not result.ok:
            # 👀 was a promise, and it was not kept. Taking it off is what
            # tells the user the difference between thinking and dead.
            await self._mark_all(batch, False)

        if result.text:
            try:
                await self._deliver(last, result.text)
            except Exception:
                # The one step that can still fail after a good answer.
                # Leaving 👀 on would say «still thinking» about a turn
                # that is over, so the promise is withdrawn here too.
                log.exception("could not deliver the answer to %s", last.message_id)
                await self._mark_all(batch, False)
