import uuid
from types import SimpleNamespace
from typing import Any

from telegrind import store
from telegrind.meta import sessions
from telegrind.meta.config import MetaConfig
from telegrind.models import VERDICT_SYSTEM, VERDICT_TALK


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
    """`parent_of` here answers something other than 10, so the assertion
    only passes if the bot-branch guard is never taken — an inverted guard
    would call this and return the wrong value instead of passing by
    accident."""

    async def parent_of(message_id: int) -> int | None:
        return 999

    assert await sessions.turn_key(msg(11, user_msg(10)), parent_of=parent_of) == 10


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


class RowSession:
    """Enough session for `store.turn_root`: a table of our own rows.

    `execute` ignores the statement and answers from the chain, which is
    all `get_message` asks of it.
    """

    def __init__(self, rows: dict[int, Any]) -> None:
        self.rows = rows
        self.asked: list[int] = []

    async def execute(self, statement: Any) -> SimpleNamespace:
        wanted = statement.compile().params["message_id_1"]
        self.asked.append(wanted)
        return SimpleNamespace(scalar_one_or_none=lambda: self.rows.get(wanted))


def row(message_id: int, verdict: str, *, is_bot: bool, reply_to: int | None) -> Any:
    return SimpleNamespace(
        message_id=message_id,
        verdict=verdict,
        raw={
            "from_user": {"is_bot": is_bot},
            **({"reply_to_message": {"message_id": reply_to}} if reply_to else {}),
        },
    )


async def test_the_host_and_the_guest_resolve_the_same_root() -> None:
    """The chain measured in the live chat on 2026-09-13: 1124 asked, 1125
    is Claude's reply to it, 1126 replies to 1125.

    Two answers to one question — which verdict does 1126 inherit, and
    which session does its turn write to — and they are computed by two
    different modules. If they ever disagree the message goes to Claude
    but into the wrong session, and comes back answered with no memory of
    the thread: the very symptom this fix exists to remove.
    """
    session = RowSession(
        {
            1125: row(1125, VERDICT_SYSTEM, is_bot=True, reply_to=1124),
            1124: row(1124, VERDICT_TALK, is_bot=False, reply_to=None),
        }
    )

    async def parent_of(message_id: int) -> int | None:
        return {1125: 1124}.get(message_id)

    guest = await sessions.turn_key(msg(1126, bot_msg(1125)), parent_of=parent_of)
    host = await store.turn_root(session, 1, 1125)

    assert host == guest == 1124


async def test_a_reply_to_ones_own_message_resolves_to_that_message() -> None:
    """No hop: the parent is the turn. Same shape as the guest's own
    `test_a_reply_to_ones_own_message_is_that_message`."""
    session = RowSession({10: row(10, VERDICT_TALK, is_bot=False, reply_to=999)})
    assert await store.turn_root(session, 1, 10) == 10


async def test_an_unknown_parent_resolves_to_itself() -> None:
    """A message from before the bot stored what it says. Falling back to
    the parent keeps the lookup total — the caller then finds no row and
    reads no verdict, which is the same as not continuing anything."""
    assert await store.turn_root(RowSession({}), 1, 10) == 10
