from aiogram import Dispatcher

from .dispatcher import dp
from .router import router


def setup_dispatcher() -> Dispatcher:
    """Import the handler modules, which is what registers them.

    Registration is subscription: importing a handler module registers its
    handlers, and registering an observer is also what subscribes its
    update type — aiogram derives allowed_updates from the handlers that
    exist. So this is the wiring, not a side effect of it.

    Two hazards live in these four lines, and `tests/test_setup.py` pins
    both.

    Order matters *within* an observer, and `handlers` ends in a filterless
    catch-all — so `query` must come first or `ask` never matches. The user
    would still get an answer (`classify.presumed` reads `/q` as a question
    whichever handler stores it), so nothing would look broken: the
    explicit override, the one that exists for when the classifier is
    wrong, would simply be dead code. Three statements rather than one
    `from .handlers import handlers, query`: ruff's isort sorts the names
    inside a single `from` and would put `handlers` in front.

    And `reactions` is not optional despite competing with nothing. aiogram
    derives allowed_updates from the handlers that exist, so dropping that
    import unsubscribes `message_reaction` — which is the delete gesture,
    the only way the user can delete anything.

    There is no ChatActionMiddleware any more: it went with the echo, and
    nothing types.
    """
    from . import middleware  # noqa: F401, I001
    from .handlers import query as query
    from .handlers import handlers as handlers
    from .handlers import reactions as reactions

    dp.include_router(router)

    return dp
