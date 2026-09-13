"""One test of `setup_dispatcher`, and it has to stay one.

`setup_dispatcher()` ends in `dp.include_router(router)`, and both are
module-level singletons — a second call raises «Router is already
attached». So everything about the *registration* is asserted in a single
pass.

`attach_meta` is split out of it for exactly that reason: it touches no
router, so it can be called as often as a test likes, and the one line in
`setup_dispatcher` worth pinning — the assignment that makes a hand-off
reachable at all — becomes testable. The tests below are that.
"""

import logging
import sys
from typing import Any

from telegrind.bot import setup
from telegrind.bot.router import router
from telegrind.bot.setup import setup_dispatcher

#: The package goes too, and it has to go first. `from .handlers import
#: query` resolves through the parent package's attributes, so dropping
#: only the submodules leaves `telegrind.bot.handlers.query` sitting on the
#: package object and the import is satisfied without running anything.
HANDLER_MODULES = (
    "telegrind.bot.handlers",
    "telegrind.bot.handlers.query",
    "telegrind.bot.handlers.handlers",
    "telegrind.bot.handlers.reactions",
)


def test_the_wiring_registers_in_order_and_subscribes_the_reaction_update(
    monkeypatch: Any,
) -> None:
    """What registration order and the import list actually buy.

    Both failures here are silent. Reorder the imports in
    `setup_dispatcher` and the filterless catch-all matches first, so `ask`
    never runs — and nothing looks broken, because `classify.presumed`
    still reads `/q` as a question whichever handler stored it. The
    explicit override, the one that exists for when the classifier is
    wrong, would just be dead code.

    Drop the `reactions` import and it is worse: aiogram derives
    allowed_updates from the handlers that exist, so `message_reaction`
    leaves the subscription and Telegram stops delivering reactions
    altogether. Tapping the 💔 is the only way to delete anything in this
    bot, and it would stop working with a fully green suite.

    Registration moved out of `handlers/__init__.py` (which now imports
    nothing, so that `telegrind.bot.routing` is importable on its own), so
    the order is guaranteed in exactly one place now instead of at every
    entry point. This test is that guarantee.

    The reset below is not ceremony. Importing a handler module registers
    it, several other test modules do that at module scope, and the router
    is a singleton — so by the time this runs it is already populated in
    pytest's *collection* order, which has nothing to do with how the bot
    wires up. Clearing the observers and dropping the three modules from
    `sys.modules` makes `setup_dispatcher` do the importing itself, which
    is what a cold `main.py` does. Without it this test passes or fails on
    the alphabet.
    """
    for observer in (router.message, router.edited_message, router.message_reaction):
        observer.handlers.clear()
    for name in HANDLER_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)

    dp = setup_dispatcher()

    assert [h.callback.__name__ for h in router.message.handlers] == ["ask", "record"]
    assert "message_reaction" in dp.resolve_used_update_types()


def a_sessionmaker() -> Any:
    """Stands in for `async_sessionmaker`. `attach_meta` only stores it."""
    return None


def test_the_hand_off_is_reachable_once_the_layer_is_attached(
    monkeypatch: Any,
) -> None:
    """`handlers.HAND_OVER` is a module global set from the outside, and a
    typo in that one line is silent: routing would log «nobody to hand
    message N to» forever and the bot would look like it simply never
    talks."""
    from telegrind.bot.handlers import handlers

    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7")
    monkeypatch.setattr(handlers, "HAND_OVER", None)

    layer = setup.attach_meta(a_sessionmaker)

    assert layer is not None
    assert layer.hand_over == handlers.HAND_OVER


def test_no_allowlist_leaves_the_recording_half_running_alone(
    monkeypatch: Any,
) -> None:
    """Off is a supported state, not a misconfiguration: without the
    allowlist the bot stores and counts exactly as it did before."""
    from telegrind.bot.handlers import handlers

    monkeypatch.delenv("CLAUDE_ADMIN_CHAT_IDS", raising=False)
    monkeypatch.setattr(handlers, "HAND_OVER", None)

    assert setup.attach_meta(a_sessionmaker) is None
    assert handlers.HAND_OVER is None


def test_a_missing_sessionmaker_is_not_blamed_on_the_allowlist(
    monkeypatch: Any, caplog: Any
) -> None:
    """Two different reasons to be off, and the log has to say which. A
    worker outlives the update that queued it and opens its own session, so
    a caller that passed no sessionmaker has a real bug — and being told
    «CLAUDE_ADMIN_CHAT_IDS is not set» about an allowlist that *is* set
    sends the next hour in the wrong direction."""
    from telegrind.bot.handlers import handlers

    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7")
    monkeypatch.setattr(handlers, "HAND_OVER", None)

    with caplog.at_level(logging.INFO, logger="telegrind.bot.setup"):
        assert setup.attach_meta(None) is None

    assert "sessionmaker" in caplog.text
    assert "CLAUDE_ADMIN_CHAT_IDS" not in caplog.text
    assert handlers.HAND_OVER is None


# --- the startup sweep ------------------------------------------------------


class Sessions:
    """Stands in for `async_sessionmaker`: called, then used as an async CM.

    `release_stranded_turns` opens its own session — it runs before
    polling, so there is no handler session to borrow — and `opened` is how
    a test says whether it got as far as touching the database at all.
    """

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> Any:
        self.opened += 1
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class Released:
    """A recorder standing in for `meta_wiring.release_hand_overs`."""

    def __init__(self, message_ids: list[int] | None = None) -> None:
        self.calls: list[frozenset[int]] = []
        self.message_ids = message_ids or []

    async def __call__(self, session: Any, admin_chat_ids: frozenset[int]) -> list[int]:
        self.calls.append(admin_chat_ids)
        return self.message_ids


async def test_the_sweep_runs_on_the_layers_own_chats(monkeypatch: Any) -> None:
    """It opens a session of its own — there is no handler session at boot —
    and it is scoped to the allowlist, because the layer cleans up after
    itself and after nothing else."""
    released = Released([10, 11])
    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7 8")
    monkeypatch.setattr(setup.meta_wiring, "release_hand_overs", released)
    sessions = Sessions()

    assert await setup.release_stranded_turns(sessions) == 2
    assert sessions.opened == 1
    assert released.calls == [frozenset({7, 8})]


async def test_the_sweep_does_not_run_when_the_layer_is_off(monkeypatch: Any) -> None:
    """Off is a supported state. Without the allowlist there is nobody to
    hand a message to, so there is no marker anyone could have left — and a
    bot that queries the database on behalf of a layer that is not running
    is a layer that is not really off."""
    released = Released()
    monkeypatch.delenv("CLAUDE_ADMIN_CHAT_IDS", raising=False)
    monkeypatch.setattr(setup.meta_wiring, "release_hand_overs", released)
    sessions = Sessions()

    assert await setup.release_stranded_turns(sessions) == 0
    assert sessions.opened == 0
    assert released.calls == []


async def test_the_sweep_needs_a_sessionmaker_like_everything_else(
    monkeypatch: Any, caplog: Any
) -> None:
    """The second way to be off, and it reaches here too: `attach_meta`
    returns None without one, so no worker exists to have stranded
    anything.

    The quiet is the assertion. Returning 0 is *not* what distinguishes the
    guard from its absence — without it the sweep calls `None()`, the
    catch-all below it turns the TypeError into a logged traceback, and the
    function returns 0 anyway. So a test that reads only the return value
    passes with the guard deleted, and a supported off state would print an
    ERROR on every boot for the life of that mistake. What the guard buys
    is a bot that says nothing, so that is what is measured: no record at
    ERROR, and the sweep never reached the database. There is nothing to
    observe «was not called» *on* here — the sessionmaker is literally
    `None` — which is exactly why the log is the only witness.
    """
    released = Released()
    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7")
    monkeypatch.setattr(setup.meta_wiring, "release_hand_overs", released)

    with caplog.at_level(logging.ERROR, logger="telegrind.bot.setup"):
        assert await setup.release_stranded_turns(None) == 0

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert released.calls == []


async def test_a_failed_sweep_does_not_keep_the_bot_from_booting(
    monkeypatch: Any, caplog: Any
) -> None:
    """A bot that will not start because a cleanup query failed is worse
    than the bug the cleanup fixes. The database may be slow, the migration
    may not have run yet, the column may be new — and none of that is a
    reason to stop recording messages."""

    async def explode(session: Any, admin_chat_ids: frozenset[int]) -> list[int]:
        raise OSError("connection refused")

    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7")
    monkeypatch.setattr(setup.meta_wiring, "release_hand_overs", explode)

    with caplog.at_level(logging.ERROR, logger="telegrind.bot.setup"):
        assert await setup.release_stranded_turns(Sessions()) == 0

    assert "connection refused" in caplog.text


async def test_a_malformed_allowlist_does_not_take_the_bot_down_with_it(
    monkeypatch: Any, caplog: Any
) -> None:
    """«It never raises» is the promise this function is called on, and
    `main.py` restates it at the call site — two places, so it has to be
    literally true rather than true of the query alone.

    `MetaConfig.from_env` calls `int(part)` per token, so a fat-fingered
    `CLAUDE_ADMIN_CHAT_IDS` is a `ValueError`. Nothing reaches here with one
    today — `setup_dispatcher` → `attach_meta` reads the same variable a few
    lines earlier and goes down first — so this is about the sentence, not
    about a live failure mode: a promise that holds only because something
    upstream crashes first is one that breaks the day the order changes.
    """
    released = Released()
    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "семь")
    monkeypatch.setattr(setup.meta_wiring, "release_hand_overs", released)
    sessions = Sessions()

    with caplog.at_level(logging.ERROR, logger="telegrind.bot.setup"):
        assert await setup.release_stranded_turns(sessions) == 0

    assert sessions.opened == 0
    assert released.calls == []
    assert "семь" in caplog.text
