import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telegrind.bot import outbound
from telegrind.models import VERDICT_SYSTEM, Chat, Entry, LoggedMessage


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

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


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

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


async def test_what_the_bot_says_is_stored_as_system() -> None:
    """`verdict` now lives on the entry, not the message row `say` also
    writes — the message is the verbatim record, the entry is what the
    queue reads."""
    session = FakeSession()
    await outbound.say(FakeBot(), session, Chat(id=1, chat_id=7), "Записал.")
    messages = [r for r in session.added if isinstance(r, LoggedMessage)]
    entries = [r for r in session.added if isinstance(r, Entry)]
    assert [m.text for m in messages] == ["Записал."]
    assert [e.verdict for e in entries] == [VERDICT_SYSTEM]


async def test_a_reply_goes_out_with_reply_parameters() -> None:
    """reply_to_message_id is deprecated at aiogram 3.27."""
    bot = FakeBot()
    await outbound.say(bot, FakeSession(), Chat(id=1, chat_id=7), "…", reply_to=42)
    assert bot.sent[0]["reply_parameters"].message_id == 42


async def test_a_plain_message_carries_no_reply_parameters() -> None:
    bot = FakeBot()
    await outbound.say(bot, FakeSession(), Chat(id=1, chat_id=7), "…")
    assert bot.sent[0].get("reply_parameters") is None
