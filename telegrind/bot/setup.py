from aiogram import Dispatcher

from .dispatcher import dp
from .router import router


def setup_dispatcher() -> Dispatcher:
    """Import the handlers, which is what registers them.

    There is no ChatActionMiddleware any more: it went with the echo, and
    nothing types.
    """
    from . import handlers, middleware  # noqa: F401

    dp.include_router(router)

    return dp
