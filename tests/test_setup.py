"""One test of `setup_dispatcher`, and it has to stay one.

`setup_dispatcher()` ends in `dp.include_router(router)`, and both are
module-level singletons — a second call raises «Router is already
attached». So everything about the *registration* is asserted in a single
pass.

What used to sit beside it — `attach_meta`, the stranded-turn sweep and
their tests — went with the Claude meta layer on 2026-09-14. Nothing sets
`handlers.HAND_OVER` any more; `routing.py` is tested with `hand_over=None`
in `tests/test_routing.py`, which is now the whole story of that seam.
"""

import sys
from typing import Any

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
