"""The hand-off, and the five callables the host supplies.

`telegrind/bot/meta_wiring.py` is the only place the two halves touch, so
it is the only place a test has to look to know what the meta layer costs
the bot.
"""

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telegrind import meta
from telegrind.bot import meta_wiring
from telegrind.bot.handlers.receipts import HANDED_OVER
from telegrind.meta.config import MetaConfig
from telegrind.meta.runtime import TurnResult
from telegrind.models import VERDICT_SYSTEM, Chat, LoggedMessage

CFG = MetaConfig(admin_chat_ids=frozenset({7}))


class FakeSession:
    """test_outbound.py's fake, plus `get` and a record of read depth.

    A hand-written fake has no transaction state, so it cannot reproduce
    SQLAlchemy's autobegin — the trap that has now cost this codebase four
    sites. What it *can* pin is the shape that avoids it: every read has to
    happen inside a `session.begin()`, so `read_depths` must never contain
    a zero. That is not proof, but it is the only automatable guard the
    suite can carry, and it catches the defect by inspection-equivalent.
    """

    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.added: list[Any] = []
        self.depth = 0
        self.read_depths: list[int] = []

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        self.read_depths.append(self.depth)
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.row, scalars=lambda: iter(())
        )

    async def get(self, model: object, pk: int) -> Any:
        self.read_depths.append(self.depth)
        return Chat(id=pk, chat_id=7)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.reactions: list[tuple[int, list[Any]]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(
            message_id=900 + len(self.sent),
            date=datetime(2026, 9, 12, 10, tzinfo=UTC),
            edit_date=None,
            text=text,
            caption=None,
            voice=None,
            forward_origin=None,
            chat=SimpleNamespace(id=chat_id),
            model_dump=lambda mode="json": {"text": text},
        )

    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: list[Any]
    ) -> None:
        self.reactions.append((message_id, reaction))


# --- the wiring -------------------------------------------------------------


async def test_parent_of_reads_the_reply_target_off_our_own_row() -> None:
    """Telegram does not nest replies, so the second hop of a session lookup
    can only come from the row the bot stored itself."""
    session = FakeSession(LoggedMessage(raw={"reply_to_message": {"message_id": 42}}))
    assert await meta_wiring.parent_of(session, 1, 900) == 42


async def test_parent_of_reads_inside_a_transaction() -> None:
    """A bare read autobegins one that never closes, and the next
    `session.begin()` then raises «a transaction is already begun». The
    handler's session goes on to claim the row, so this read cannot be the
    one that poisons it."""
    session = FakeSession(LoggedMessage(raw={}))
    await meta_wiring.parent_of(session, 1, 900)
    assert session.read_depths == [1]


async def test_claim_records_the_hand_over_on_the_row() -> None:
    """👀 the bubble is placed when the turn starts, but 👀 the *column* is
    written now — it is the point of no return an edit checks, and an edit
    can arrive while the turn is still behind another one in the queue."""
    row = LoggedMessage(raw={}, receipt_emoji=None)
    session = FakeSession(row)
    await meta_wiring.claim(session, 1, 10)
    assert row.receipt_emoji == HANDED_OVER
    assert session.read_depths == [1]


async def test_set_receipt_puts_the_eyes_on_the_row_and_the_message() -> None:
    row = LoggedMessage(raw={}, receipt_emoji=None)
    session, bot = FakeSession(row), FakeBot()
    await meta_wiring.set_receipt(bot, session, 7, 1, 10, True)
    assert row.receipt_emoji == HANDED_OVER
    assert bot.reactions[0][1][0].emoji == HANDED_OVER


async def test_set_receipt_takes_the_eyes_off_both_when_the_turn_failed() -> None:
    """An empty reaction list is how setMessageReaction clears. The row has
    to lose 👀 too, or the edit path goes on refusing to re-route a message
    nothing is working on any more."""
    row = LoggedMessage(raw={}, receipt_emoji=HANDED_OVER)
    session, bot = FakeSession(row), FakeBot()
    await meta_wiring.set_receipt(bot, session, 7, 1, 10, False)
    assert row.receipt_emoji is None
    assert bot.reactions[0][1] == []


async def test_claude_always_speaks_as_a_reply() -> None:
    """Without reply parameters a reply to Claude has no parent row to point
    at, and every second turn would start a new conversation."""
    session, bot = FakeSession(), FakeBot()
    await meta_wiring.speak(bot, session, 1, "Потому что.", 10)
    assert bot.sent[0]["reply_parameters"].message_id == 10
    assert [r.verdict for r in session.added] == [VERDICT_SYSTEM]


async def test_a_model_authored_answer_goes_out_unparsed() -> None:
    """The bot's default parse mode is HTML, and this is the first code that
    sends text nobody wrote by hand. `<` in an answer about code is not a
    tag, and Telegram answers «can't parse entities» with a 400 — losing the
    whole answer to make one angle bracket bold."""
    session, bot = FakeSession(), FakeBot()
    await meta_wiring.speak(bot, session, 1, "если x < 3 и y > 4", 10)
    assert bot.sent[0]["parse_mode"] is None


async def test_an_answer_longer_than_a_telegram_message_is_truncated() -> None:
    """4096 is a hard Bot API limit. Splitting is not the answer — a long
    reply that does not fit a conversation leaves the chat as an object —
    so until that exists the truncation is at least visible."""
    session, bot = FakeSession(), FakeBot()
    await meta_wiring.speak(bot, session, 1, "я" * 5000, 10)
    assert len(bot.sent[0]["text"]) == 4096


# --- the hand-off -----------------------------------------------------------


class Wiring:
    """The five callables, as recorders in one order-preserving list."""

    def __init__(self, parent: int | None = None) -> None:
        self.events: list[str] = []
        self.parent = parent

    async def parent_of(
        self, session: Any, chat_pk: int, message_id: int
    ) -> int | None:
        self.events.append(f"parent_of:{message_id}")
        return self.parent

    async def claim(self, session: Any, chat_pk: int, message_id: int) -> None:
        self.events.append(f"claim:{message_id}")

    async def set_receipt(
        self,
        bot: Any,
        session: Any,
        chat_id: int,
        chat_pk: int,
        message_id: int,
        started: bool,
    ) -> None:
        self.events.append(f"receipt:{message_id}:{started}")

    async def speak(
        self, bot: Any, session: Any, chat_pk: int, text: str, reply_to: int
    ) -> None:
        self.events.append(f"speak:{reply_to}:{text}")


class FakeSessionmaker:
    """A worker outlives the update that queued it, so it opens its own."""

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> Any:
        self.opened += 1

        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            yield FakeSession()

        return ctx()


def message(message_id: int, text: str = "почему", reply_to: Any = None) -> Any:
    return SimpleNamespace(
        message_id=message_id, text=text, caption=None, reply_to_message=reply_to
    )


def layer_for(wiring: Wiring, maker: FakeSessionmaker) -> meta.MetaLayer:
    return meta.MetaLayer(
        CFG,
        async_session=maker,
        parent_of=wiring.parent_of,
        claim=wiring.claim,
        set_receipt=wiring.set_receipt,
        speak=wiring.speak,
    )


async def test_a_chat_off_the_allowlist_is_refused_without_touching_the_row() -> None:
    """False is not an error — it is «routing should answer as if there were
    no meta layer». The gate is on the chat, not on the verdict, so talk
    from a stranger is not answered at all."""
    wiring, maker = Wiring(), FakeSessionmaker()
    layer = layer_for(wiring, maker)
    accepted = await layer.hand_over(
        message(10), None, SimpleNamespace(id=1, chat_id=999), FakeSession(), FakeBot()
    )
    assert accepted is False
    assert wiring.events == []


async def test_the_hand_over_is_recorded_on_the_row_before_it_is_queued(
    monkeypatch: Any,
) -> None:
    """The 👀 ordering, and the reason it is not academic. `hand_over`
    returns True and `record` finishes; if the row does not yet say
    HANDED_OVER, an edit arriving next sees a message that was never handed
    over, re-classifies it and hands it over a second time. The window is
    not milliseconds — a message queued behind a running turn on the same
    key waits for the whole of that turn."""
    ran: list[str] = []

    async def fake_run(cfg: Any, prompt: str, **kwargs: Any) -> TurnResult:
        ran.append(prompt)
        return TurnResult(text="Потому что.", ok=True)

    monkeypatch.setattr(meta, "run_turn", fake_run)
    wiring, maker = Wiring(), FakeSessionmaker()

    # The queue has to be observable here, not just its effects: `submit`
    # creates a task and never yields, so a claim written *after* it still
    # lands before `hand_over` returns and an effects-only assertion cannot
    # tell the two orders apart. Verified by mutation on 2026-09-12.
    class Recording(meta.Turns):
        async def submit(self, job: Any) -> None:
            wiring.events.append(f"submit:{job.message_id}")
            await super().submit(job)

    monkeypatch.setattr(meta, "Turns", Recording)
    layer = layer_for(wiring, maker)

    accepted = await layer.hand_over(
        message(10), None, SimpleNamespace(id=1, chat_id=7), FakeSession(), FakeBot()
    )
    assert accepted is True
    # Claimed before the queue is told, and so before hand_over returned.
    assert wiring.events == ["claim:10", "submit:10"]

    await layer.turns.drain()
    assert wiring.events[2:] == ["receipt:10:True", "speak:10:Потому что."]
    assert ran == ["почему"]


async def test_a_reply_to_a_claude_message_continues_that_turn(
    monkeypatch: Any,
) -> None:
    """Two hops: the parent is a bot message, so the turn is whatever *it*
    replied to — read out of our own row, because Telegram does not nest."""

    async def fake_run(cfg: Any, prompt: str, **kwargs: Any) -> TurnResult:
        return TurnResult(text="ок", ok=True)

    monkeypatch.setattr(meta, "run_turn", fake_run)
    wiring, maker = Wiring(parent=10), FakeSessionmaker()
    layer = layer_for(wiring, maker)

    answer = SimpleNamespace(message_id=900, from_user=SimpleNamespace(is_bot=True))
    await layer.hand_over(
        message(11, reply_to=answer),
        None,
        SimpleNamespace(id=1, chat_id=7),
        FakeSession(),
        FakeBot(),
    )
    await layer.turns.drain()
    assert wiring.events[0] == "parent_of:900"


async def test_a_worker_never_borrows_the_handler_session(
    monkeypatch: Any,
) -> None:
    """The update that queued a turn is long gone by the time it answers —
    aiogram runs handlers fire-and-forget — so the receipt and the reply
    each open a session of their own."""

    async def fake_run(cfg: Any, prompt: str, **kwargs: Any) -> TurnResult:
        return TurnResult(text="ок", ok=True)

    monkeypatch.setattr(meta, "run_turn", fake_run)
    wiring, maker = Wiring(), FakeSessionmaker()
    layer = layer_for(wiring, maker)
    await layer.hand_over(
        message(10), None, SimpleNamespace(id=1, chat_id=7), FakeSession(), FakeBot()
    )
    await layer.turns.drain()
    assert maker.opened == 2  # one for the receipt, one for the answer
