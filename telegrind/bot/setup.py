from aiogram import Dispatcher

from .dispatcher import dp
from .router import router


def setup_dispatcher() -> Dispatcher:
    """Import the handler modules, which is what registers them.

    Registration is subscription: importing a handler module registers its
    handlers, and registering an observer is also what subscribes its
    update type — aiogram derives allowed_updates from the handlers that
    exist. So this is the wiring, not a side effect of it.

    Order matters only *within* an observer, and `handlers` ends in a
    filterless catch-all message handler — so `query` must be imported
    first or /q is swallowed and stored as an ordinary message. Three
    statements rather than one `from .handlers import handlers, query`:
    ruff's isort sorts the names inside a single `from` and would put
    `handlers` in front. `reactions` is a different observer and does not
    compete.

    There is no ChatActionMiddleware any more: it went with the echo, and
    nothing types.
    """
    from . import middleware  # noqa: F401, I001
    from .handlers import query as query
    from .handlers import handlers as handlers
    from .handlers import reactions as reactions

    dp.include_router(router)

    return dp
