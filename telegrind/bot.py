"""aiogram wiring: five handlers, each a line (SPEC REQ-REACT-4, REQ-TRIGGER-6)."""

from __future__ import annotations

from aiogram import Dispatcher, Router
from aiogram.types import CallbackQuery, Message, MessageReactionUpdated, PollAnswer

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

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        await app.on_callback(query)

    @router.poll_answer()
    async def on_poll_answer(answer: PollAnswer) -> None:
        await app.on_poll_answer(answer)

    dp = Dispatcher()
    dp.include_router(router)
    return dp
