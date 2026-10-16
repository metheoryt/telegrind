"""aiogram wiring: three handlers, each a line (SPEC REQ-REACT-4)."""

from __future__ import annotations

from aiogram import Dispatcher, Router
from aiogram.types import Message, MessageReactionUpdated

from telegrind.app import App


def build_dispatcher(app: App) -> Dispatcher:
    router = Router(name="telegrind")

    @router.message()
    async def on_message(message: Message) -> None:
        await app.on_message(message)

    @router.edited_message()
    async def on_edit(message: Message) -> None:
        await app.on_edit(message)

    @router.message_reaction()  # registering this is what subscribes message_reaction
    async def on_reaction(update: MessageReactionUpdated) -> None:
        await app.on_reaction(update)

    dp = Dispatcher()
    dp.include_router(router)
    return dp
