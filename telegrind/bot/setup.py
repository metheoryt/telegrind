from .router import router
from .dispatcher import dp
from aiogram import Dispatcher
from aiogram.utils.chat_action import ChatActionMiddleware


def setup_dispatcher() -> Dispatcher:
    from . import handlers  # noqa
    from . import middleware  # noqa

    router.message.middleware(ChatActionMiddleware())
    dp.include_router(router)

    return dp
