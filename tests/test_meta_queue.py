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


async def test_a_second_message_after_the_queue_drained_starts_a_new_worker() -> None:
    """A chat runs for months, so a finished worker drops its slot. The next
    message has to rebuild it or the bot goes quiet for good."""
    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    await turns.submit(job(20, key=20))
    await turns.drain()
    assert len(spy.prompts) == 2
