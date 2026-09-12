import uuid
from types import SimpleNamespace
from typing import Any

from telegrind.meta import sessions
from telegrind.meta.config import MetaConfig


def msg(message_id: int, parent: Any = None) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, reply_to_message=parent)


def bot_msg(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(is_bot=True),
        reply_to_message=None,
    )


def user_msg(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(is_bot=False),
        reply_to_message=None,
    )


async def nothing(message_id: int) -> int | None:
    return None


def test_the_session_id_is_derived_and_stable() -> None:
    """Nothing is stored to make sessions work, so the session graph cannot
    drift from the chat."""
    first = sessions.session_id(7, 10)
    assert first == sessions.session_id(7, 10)
    assert first != sessions.session_id(7, 11)
    assert first != sessions.session_id(8, 10)
    assert isinstance(first, uuid.UUID)


async def test_a_plain_message_is_its_own_turn() -> None:
    """No parent, so it starts a new conversation — which falls out of the
    mechanism instead of needing a rule."""
    assert await sessions.turn_key(msg(10), parent_of=nothing) == 10


async def test_a_reply_to_ones_own_message_is_that_message() -> None:
    assert await sessions.turn_key(msg(11, user_msg(10)), parent_of=nothing) == 10


async def test_a_reply_to_a_bot_message_is_what_the_bot_replied_to() -> None:
    """Telegram does not nest replies, so the second hop reads our own row
    for the bot's message — which exists because the bot stores what it
    says."""

    async def parent_of(message_id: int) -> int | None:
        return {901: 10}.get(message_id)

    assert await sessions.turn_key(msg(11, bot_msg(901)), parent_of=parent_of) == 10


async def test_a_bot_message_with_no_stored_parent_is_its_own_turn() -> None:
    """A bot message sent before the outbound store existed, or one sent
    without reply parameters. Starting a fresh conversation is the honest
    failure."""
    assert await sessions.turn_key(msg(11, bot_msg(901)), parent_of=nothing) == 901


def test_no_allowlist_means_no_meta_layer(monkeypatch: Any) -> None:
    monkeypatch.delenv("CLAUDE_ADMIN_CHAT_IDS", raising=False)
    assert MetaConfig.from_env() is None


def test_the_allowlist_is_read_off_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7, 8")
    cfg = MetaConfig.from_env()
    assert cfg is not None
    assert cfg.allows(7) and cfg.allows(8) and not cfg.allows(9)
