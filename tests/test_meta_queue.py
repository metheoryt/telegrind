"""One turn at a time per session key, and everything else concurrent."""

import asyncio
import uuid
from typing import Any

from telegrind.meta.config import MetaConfig
from telegrind.meta.queue import Job, Turns
from telegrind.meta.runtime import TurnResult

CFG = MetaConfig(admin_chat_ids=frozenset({7}))


def job(message_id: int, key: int, text: str = "x") -> Job:
    return Job(chat_id=7, chat_pk=1, message_id=message_id, text=text, key=key)


class Spy:
    def __init__(
        self,
        result: str = "ok",
        gate: asyncio.Event | None = None,
        ok: bool = True,
        exit_code: int | None = 0,
    ) -> None:
        self.prompts: list[str] = []
        #: (session_id, resume_from) per call, so a test can see whether the
        #: retry really dropped the resume.
        self.calls: list[tuple[uuid.UUID, uuid.UUID | None]] = []
        self.delivered: list[tuple[str, int]] = []
        self.marked: list[tuple[int, bool]] = []
        self.result = result
        self.gate = gate
        self.ok = ok
        self.exit_code = exit_code

    async def run(
        self, prompt: str, *, session_id: uuid.UUID, resume_from: uuid.UUID | None
    ) -> TurnResult:
        self.prompts.append(prompt)
        self.calls.append((session_id, resume_from))
        if self.gate is not None:
            await self.gate.wait()
        return TurnResult(text=self.result, ok=self.ok, exit_code=self.exit_code)

    async def deliver(self, job: Job, text: str) -> None:
        self.delivered.append((text, job.message_id))

    async def mark(self, job: Job, started: bool) -> None:
        self.marked.append((job.message_id, started))


async def test_a_turn_is_marked_when_it_starts_not_when_it_is_queued() -> None:
    """👀 goes on when the message is handed to the process, so the queue is
    legible on screen: the messages still bare are the ones not yet seen."""
    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.marked[0] == (10, True)


async def test_the_answer_replies_to_the_message_that_caused_it() -> None:
    spy = Spy(result="Потому что.")
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.delivered == [("Потому что.", 10)]


async def test_two_messages_queued_before_the_turn_starts_go_in_together() -> None:
    """A correction sent two seconds later belongs to the same thought, and
    one turn seeing both answers better than two turns each seeing half."""
    gate = asyncio.Event()
    spy = Spy(gate=gate)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10, text="первое"))
    await turns.submit(job(11, key=10, text="второе"))
    gate.set()
    await turns.drain()

    assert len(spy.prompts) == 1
    assert "первое" in spy.prompts[0] and "второе" in spy.prompts[0]
    assert {m for m, _ in spy.marked} == {10, 11}
    assert spy.delivered == [("ok", 11)]


async def test_two_different_keys_run_concurrently() -> None:
    """Different fork points are different sessions; serialising them would
    merge two threads the user deliberately split."""
    gate = asyncio.Event()
    spy = Spy(gate=gate)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.submit(job(20, key=20))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(spy.prompts) == 2
    gate.set()
    await turns.drain()


async def test_a_failed_turn_takes_the_eyes_off() -> None:
    async def failing(prompt: str, **kwargs: Any) -> TurnResult:
        return TurnResult(text="Упал.", ok=False)

    spy = Spy()
    turns = Turns(CFG, run=failing, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.marked[-1] == (10, False)
    assert spy.delivered == [("Упал.", 10)]


async def test_a_message_replying_to_an_earlier_turn_resumes_that_session() -> None:
    """The key names a turn that is not in this batch, so there is a session
    to fork off — that is the whole reason `resume_from` exists."""
    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(11, key=10))
    await turns.drain()
    assert spy.calls[0][1] is not None


async def test_a_process_that_refused_before_the_turn_is_retried_cold() -> None:
    """The escape hatch. A merged batch names its session after the *last*
    message, so the key's own id was never written and `--resume` fails
    before any model call — exit 1, nothing on stdout. Starting fresh loses
    the thread; a crash string loses the answer."""
    spy = Spy(result="Упал с кодом 1: No conversation found", ok=False, exit_code=1)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(11, key=10))
    await turns.drain()

    assert len(spy.calls) == 2
    assert spy.calls[0][1] is not None
    assert spy.calls[1][1] is None
    # Both turns write the same id: the retry is the same turn, run again.
    assert spy.calls[0][0] == spy.calls[1][0]


async def test_a_turn_that_ran_and_failed_is_not_retried_cold() -> None:
    """An empty-but-exit-0 turn is `ok=False` since Task 8, and the process
    got far enough to answer — so the resume target was alive and a cold
    retry cannot change the outcome. It would cost a full cold session
    (~$0.21 against 20k cache-creation tokens, measured 2026-09-12) to
    produce the same emptiness twice."""
    spy = Spy(result="Ответ пришёл пустым.", ok=False, exit_code=0)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(11, key=10))
    await turns.drain()

    assert spy.calls[0][1] is not None  # it really did pass a resume
    assert len(spy.calls) == 1
    assert spy.delivered == [("Ответ пришёл пустым.", 11)]


async def test_a_turn_that_never_started_is_not_retried_cold() -> None:
    """No exit code at all means the process could not start or was killed
    on the timeout. Neither is about the resume target, and retrying a
    timeout costs a second full timeout."""
    spy = Spy(result="Не уложился.", ok=False, exit_code=None)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(11, key=10))
    await turns.drain()
    assert len(spy.calls) == 1


async def test_a_turn_that_raises_is_said_out_loud_not_swallowed() -> None:
    """A worker task that dies takes its queue with it, and 👀 stays on a
    message nothing is working on."""

    async def exploding(prompt: str, **kwargs: Any) -> TurnResult:
        raise RuntimeError("боом")

    spy = Spy()
    turns = Turns(CFG, run=exploding, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()

    assert spy.marked[-1] == (10, False)
    assert len(spy.delivered) == 1
    assert "боом" in spy.delivered[0][0]


async def test_a_delivery_that_blows_up_takes_the_eyes_off() -> None:
    """Sending is the one step that can fail after a good answer — Telegram
    rejects an entity, the network blips — and 👀 left on says «still
    thinking» about a turn that is over."""

    async def boom(job: Job, text: str) -> None:
        raise RuntimeError("Bad Request: can't parse entities")

    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=boom, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.marked[-1] == (10, False)


async def test_a_finished_worker_leaves_no_slot_behind() -> None:
    """The pop in `_work`'s `finally` is a leak fix, and only that.

    `_queues` and `_workers` are keyed by (chat_id, turn key), and the turn
    key is a message_id — so a chat that talks for months coins a new key
    on every message and both dicts grow by one entry each, forever, with
    nothing ever reading them again.

    It is *not* what keeps the bot talking: `submit` restarts a finished
    worker through its own `worker.done()` branch, on the queue it finds or
    the one it creates, pop or no pop. The second half below pins that the
    slot rebuilds, so the leak fix cannot be mistaken for a lifeline.
    """
    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    # Awaiting the worker directly rather than `drain()`, which prunes
    # `_workers` itself — through it the `_workers` assertion below would
    # hold whatever the worker did, and half the test would be decoration.
    await turns._workers[(7, 10)]

    assert turns._queues == {}
    assert turns._workers == {}

    # Same key, so this is the slot that was just dropped being rebuilt.
    await turns.submit(job(11, key=10))
    await turns.drain()
    assert len(spy.prompts) == 2


async def test_a_message_already_in_flight_is_not_turned_twice() -> None:
    """The residual window in `route`'s question arm, closed.

    `record` commits `receipt_emoji = None` and the question arm then sits
    in `answer_for` for two model calls before `claim` runs. An edit landing
    in there finds a bare row, `record_edited`'s `handed_over` gate lets it
    through, and the same message is handed over a second time. Measured:
    the worker needs two event-loop ticks to enter `_turn` and close the
    batch, so the second hand-off does not merge — it becomes a second turn.
    Two answers to one message, and two `claude -p --session-id <uuid>`
    processes on the *same* uuid, because both derive it from one
    message_id.
    """
    gate = asyncio.Event()
    spy = Spy(gate=gate)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10, text="вопрос"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(spy.prompts) == 1  # the worker really is inside the turn

    await turns.submit(job(10, key=10, text="вопрос"))
    gate.set()
    await turns.drain()

    assert len(spy.calls) == 1  # one process, one session id
    assert spy.delivered == [("ok", 10)]


async def test_a_signal_killed_process_is_not_retried_cold() -> None:
    """A negative exit code is the machine talking, not the session.

    `runtime._spawn` returns `proc.returncode or 0`, and a process killed
    by a signal reports the negative of it — -9 for SIGKILL, whose most
    plausible source here is the OOM killer. That is truthy, so a predicate
    written as «not 0 and not None» reads it as a `--resume` naming a
    session that is not there and buys a full cold retry: 20k
    cache-creation tokens and a second `claude` process, doubling exactly
    the memory pressure that killed the first one.
    """
    spy = Spy(result="Упал с кодом -9.", ok=False, exit_code=-9)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(11, key=10))
    await turns.drain()

    assert spy.calls[0][1] is not None  # it really did pass a resume
    assert len(spy.calls) == 1


async def test_a_receipt_that_blows_up_does_not_strand_the_queue() -> None:
    """Taking 👀 *off* is the step that can fail with work still waiting.

    The receipt is a cue; the turn is the work. Unguarded, a Telegram
    outage or a database hiccup while clearing 👀 propagates out of `_turn`
    and kills the worker — and because the failure happens with a job
    already queued behind this one on the same key, `_work`'s `finally`
    finds a non-empty queue, keeps the slot, and that job waits for a
    message that may never come. Two losses from one cosmetic failure: this
    turn's answer, and the next turn entirely.
    """
    gate = asyncio.Event()
    spy = Spy(gate=gate, ok=False, result="Упал.")

    async def boom(job: Job, started: bool) -> None:
        if not started:
            raise RuntimeError("Telegram is down")
        await spy.mark(job, started)

    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=boom)
    await turns.submit(job(10, key=10))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(spy.prompts) == 1  # the turn is running, so 11 queues behind it

    await turns.submit(job(11, key=10))
    gate.set()
    await turns.drain()

    assert len(spy.prompts) == 2  # the job behind it was not stranded
    assert spy.delivered == [("Упал.", 10), ("Упал.", 11)]


async def test_a_receipt_that_blows_up_on_the_way_in_does_not_lose_the_turn() -> None:
    """The other end of the same guard: 👀 going *on*, before the run.

    Unguarded this one never reaches `self._run` at all, so the failure to
    place a reaction costs the whole answer.
    """

    async def boom(job: Job, started: bool) -> None:
        raise RuntimeError("Telegram is down")

    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=boom)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.delivered == [("ok", 10)]
