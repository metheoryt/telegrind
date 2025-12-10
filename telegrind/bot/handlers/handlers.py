import asyncio
import logging

import marvin
from aiogram import F, flags
from aiogram.types import Message, ReactionTypeEmoji
from gspread_asyncio import AsyncioGspreadClient, AsyncioGspreadSpreadsheet

from telegrind.bot.const import TIP_TEXT
from telegrind.bot.router import router
from telegrind.models import Chat
from telegrind.services.expense import ExpenseService
from telegrind.sheets import Loan, Outcome, Wish

log = logging.getLogger(__name__)


# marvin.settings.agent_model = "google-gla:gemini-2.5-flash"
log.info("marvin default model is %s", marvin.defaults.model)


@router.message(F.text.regexp(Loan.pattern))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_loan(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    await Loan(ags).record(message)
    return await message.react([ReactionTypeEmoji(emoji="👌")])


@router.message(F.text.regexp(Wish.pattern))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_wish(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    await Wish(ags).record(message)
    return await message.react([ReactionTypeEmoji(emoji="👌")])


@router.edited_message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def update_changed_message(
    edited_message: Message, agc: AsyncioGspreadClient, chat: Chat
):
    wb: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)

    # first, check for expense
    service = ExpenseService(wb)
    expense_exists = await service.expense_exists(edited_message)
    if expense_exists:
        edited_expense = await service.extract_expense(edited_message)
        # reply_text = await service.make_reply_text(edited_message, edited_expense)
        updated = await service.update_expense(edited_message, edited_expense)
        if updated:
            return await edited_message.react([ReactionTypeEmoji(emoji="✍")])
            # return await edited_message.reply(reply_text, disable_notification=True)

    # second, check for other legacy types
    for sheet in (Loan(wb), Wish(wb)):
        cell = await sheet.search_row(edited_message.message_id)
        if cell:
            row = await sheet.make_row(edited_message)
            await sheet.change_row(cell.row, row)
            return await edited_message.react([ReactionTypeEmoji(emoji="✍")])

    return await edited_message.reply("Отсутствует в книге...")


@router.message(F.reply_to_message.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def delete_record(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    bot = message.bot
    if message.text.strip() == "-":
        # delete record
        msg: Message = message.reply_to_message
        ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
        for sheet in (Outcome(ags), Loan(ags), Wish(ags)):
            cell = await sheet.search_row(msg.message_id)
            if cell:
                await sheet.delete_row(cell.row)
                return await asyncio.gather(
                    bot.set_message_reaction(
                        chat_id=msg.chat.id,
                        message_id=msg.message_id,
                        reaction=[ReactionTypeEmoji(emoji="💩")],
                    ),
                    bot.set_message_reaction(
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        reaction=[ReactionTypeEmoji(emoji="👌")],
                    ),
                )
        return await msg.reply("Отсутствует в книге...")


@router.message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_outcome_llm(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    # support AI parsing only for expenses for now
    is_expense = await ExpenseService.is_expense(message.text)
    if not is_expense:
        return await message.reply(TIP_TEXT)

    wb: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)

    service = ExpenseService(wb)
    expense = await service.extract_expense(message)  # parse expense from text
    # reply_text = await service.make_reply_text(message, expense)  # talk about it

    # write it to the worksheet at last
    expense = await service.add_expense(message, expense)
    return await message.react([ReactionTypeEmoji(emoji="👌")])
