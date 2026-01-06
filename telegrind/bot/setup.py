from aiogram import Dispatcher
from aiogram.utils.chat_action import ChatActionMiddleware

from .dispatcher import dp
from .router import router


def setup_dispatcher() -> Dispatcher:
    from . import handlers  # noqa
    from . import middleware  # noqa

    router.message.middleware(ChatActionMiddleware())
    dp.include_router(router)

    return dp
