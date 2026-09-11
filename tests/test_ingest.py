import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telegrind import extract
from telegrind.bot.handlers import handlers
from telegrind.bot.handlers.handlers import (
    RECEIPT_CYCLE,
    RECEIPT_EMOJI,
    acknowledge,
    next_receipt,
)
from telegrind.config import ChatConfig
from telegrind.models import (
    KIND_TEXT,
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    Chat,
    LoggedMessage,
)

CFG = ChatConfig(tz_offset=6, currency="KZT")


class FakeBot:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, int, list[str]]] = []

    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: list[Any]
    ) -> None:
        self.reactions.append((chat_id, message_id, [r.emoji for r in reaction]))


def message(message_id: int = 4821) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 9, 9, 15, 40, tzinfo=UTC),
        edit_date=None,
        text="4500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=3260987),
        model_dump=lambda mode=None: {"message_id": message_id},
    )


def test_the_receipt_is_a_broken_heart() -> None:
    assert RECEIPT_EMOJI == "💔"


async def test_acknowledge_sets_exactly_one_reaction() -> None:
    """A bot that sets two gets REACTIONS_TOO_MANY."""
    bot = FakeBot()
    await acknowledge(bot, chat_id=3260987, message_id=4821, emoji=RECEIPT_EMOJI)
    assert bot.reactions == [(3260987, 4821, ["💔"])]


async def test_acknowledge_survives_a_telegram_failure() -> None:
    """The receipt is cosmetic; the message row is already committed."""

    class Failing(FakeBot):
        async def set_message_reaction(
            self, chat_id: int, message_id: int, reaction: list[Any]
        ) -> None:
            raise RuntimeError("Bad Request: REACTION_INVALID")

    await acknowledge(Failing(), chat_id=1, message_id=2, emoji=RECEIPT_EMOJI)


def test_every_emoji_in_the_cycle_is_a_broken_or_burning_heart() -> None:
    """Any user reaction deletes, so the cycle must not stop looking like one.

    Telegram will not let the picker be narrowed, so the emoji the bot
    places is the only thing telling the user what a tap does. All three
    were checked against setMessageReaction on 2026-09-11.
    """
    assert RECEIPT_CYCLE == ("💔", "❤\u200d🔥", "💘")
    assert RECEIPT_CYCLE[0] == RECEIPT_EMOJI


def test_an_edit_advances_the_cycle() -> None:
    assert next_receipt("💔") == "❤\u200d🔥"
    assert next_receipt("❤\u200d🔥") == "💘"


def test_the_cycle_wraps() -> None:
    assert next_receipt("💘") == RECEIPT_CYCLE[0]


def test_a_message_from_before_the_column_is_assumed_to_carry_the_default() -> None:
    """Rows stored by the previous version have no receipt_emoji.

    They visibly carry 💔, because that is all the old code ever placed,
    so the first edit must move off it rather than re-place it.
    """
    assert next_receipt(None) == "❤\u200d🔥"


def test_an_emoji_that_is_no_longer_in_the_cycle_still_changes() -> None:
    """The point of the reaction is that an edit looks different."""
    assert next_receipt("👍") != "👍"


class EditSession:
    """Enough session for record_edited: one lookup, reused by the upsert."""

    def __init__(self, existing: LoggedMessage | None) -> None:
        self.existing = existing
        self.added: list[LoggedMessage] = []

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            yield

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        row = self.existing
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def stored(*, extracted: bool, verdict: str = VERDICT_FACT) -> LoggedMessage:
    return LoggedMessage(
        id=42,
        chat_pk=1,
        message_id=10,
        kind=KIND_TEXT,
        text="4500 такси",
        tg_date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        raw={},
        receipt_emoji=RECEIPT_EMOJI,
        extracted_at=datetime(2026, 9, 11, 4, tzinfo=UTC) if extracted else None,
        verdict=verdict,
    )


def edit() -> SimpleNamespace:
    return SimpleNamespace(
        message_id=10,
        chat=SimpleNamespace(id=7),
        text="5500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        edit_date=1789094740,
        model_dump=lambda mode="json": {},
    )


async def test_an_edit_of_an_extracted_message_re_extracts_it(
    monkeypatch: Any,
) -> None:
    called: list[int] = []

    async def fake_run_for(
        session: object, chat: object, cfg: object, row: Any, **kwargs: object
    ) -> SimpleNamespace:
        called.append(row.id)
        return SimpleNamespace(facts=1, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=True)),
        CFG,
        FakeBot(),
    )

    assert called == [42]


async def test_an_edit_of_an_unextracted_message_makes_no_call(
    monkeypatch: Any,
) -> None:
    called: list[int] = []

    async def fake_run_for(
        session: object, chat: object, cfg: object, row: Any, **kwargs: object
    ) -> SimpleNamespace:
        called.append(row.id)
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=False)),
        CFG,
        FakeBot(),
    )

    assert called == []


async def test_editing_a_command_leaves_it_out_of_the_extractor(
    monkeypatch: Any,
) -> None:
    called: list[int] = []

    async def fake_run_for(
        session: object, chat: object, cfg: object, row: Any, **kwargs: object
    ) -> SimpleNamespace:
        called.append(row.id)
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=False)
    existing.text = "/q сколкьо я потратил"
    existing.extractable = False
    message = edit()
    message.text = "/q сколько я потратил"

    await handlers.record_edited(
        message, Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert existing.extractable is False
    assert called == []


class NewMessageSession:
    """Enough session for record_command / record_voice / record_text: no
    existing row, so upsert_message always inserts."""

    def __init__(self) -> None:
        self.added: list[LoggedMessage] = []

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            yield

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def command_message(text: str = "/start") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=99,
        date=datetime(2026, 9, 9, 15, 40, tzinfo=UTC),
        edit_date=None,
        text=text,
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=3260987),
        model_dump=lambda mode=None: {"message_id": 99},
    )


async def test_a_non_q_command_gets_the_system_verdict() -> None:
    """COMMAND_LIKE is a text-prefix filter, not a registered-command one:
    /start, /help and a typo all reach record_command live. Once the tail
    reads verdict instead of extractable, leaving these on the default fact
    verdict would put them right back in the extraction tail."""
    session = NewMessageSession()

    await handlers.record_command(
        command_message("/start"), Chat(id=1, chat_id=7), session, FakeBot()
    )

    assert session.added[0].extractable is False
    assert session.added[0].verdict == VERDICT_SYSTEM


async def test_a_plain_message_still_gets_the_fact_verdict() -> None:
    """_store's other two callers (voice, the catch-all) must keep writing
    the fact default — only the command path changes."""
    session = NewMessageSession()

    await handlers.record_text(message(), Chat(id=1, chat_id=7), session, FakeBot())

    assert session.added[0].extractable is True
    assert session.added[0].verdict == VERDICT_FACT


async def test_editing_a_q_row_does_not_flip_its_verdict(monkeypatch: Any) -> None:
    """Editing a /q row still starts with '/', so `parses` is False here too
    — but the verdict must stay VERDICT_QUESTION, not fall back to the
    fact/system default that a re-derivation would produce."""

    async def fake_run_for(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=False, verdict=VERDICT_QUESTION)
    existing.text = "/q сколкьо я потратил"
    existing.extractable = False
    edited = edit()
    edited.text = "/q сколько я потратил"

    await handlers.record_edited(
        edited, Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert existing.verdict == VERDICT_QUESTION


async def test_editing_a_message_with_no_prior_row_keeps_the_fact_default(
    monkeypatch: Any,
) -> None:
    """A row upsert_message has never seen before has no verdict to
    preserve, so it keeps the ordinary first-sighting default."""

    async def fake_run_for(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    session = EditSession(None)
    await handlers.record_edited(edit(), Chat(id=1, chat_id=7), session, CFG, FakeBot())

    assert session.added[0].verdict == VERDICT_FACT
