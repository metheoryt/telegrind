from aiogram import flags, F
from aiogram.types import Message
from gspread_asyncio import AsyncioGspreadSpreadsheet, AsyncioGspreadClient

from telegrind.models import Chat
from telegrind.sheets import Outcome, Loan, Wish
from telegrind.bot.router import router
from telegrind.bot.const import TIP_TEXT


@router.message(F.text.regexp(Outcome.pattern))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_outcome(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    await Outcome(ags).record(message)
    return await message.reply("Записала!")


@router.message(F.text.regexp(Loan.pattern))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_loan(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    await Loan(ags).record(message)
    return await message.reply("Записала!")


@router.message(F.text.regexp(Wish.pattern))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_wish(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    await Wish(ags).record(message)
    return await message.reply("Записала!")


@router.edited_message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def update_changed_message(
    edited_message: Message, agc: AsyncioGspreadClient, chat: Chat
):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    for sheet in (Outcome(ags), Loan(ags), Wish(ags)):
        cell = await sheet.search_row(edited_message.message_id)
        if cell:
            row = await sheet.make_row(edited_message)
            await sheet.change_row(cell.row, row)
            return await edited_message.reply("Поправила!")

    return await edited_message.reply("Не нашла этого в книге...")


@router.message(F.reply_to_message.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def delete_record(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    if message.text.strip() == "-":
        # delete record
        msg: Message = message.reply_to_message
        ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
        for sheet in (Outcome(ags), Loan(ags), Wish(ags)):
            cell = await sheet.search_row(msg.message_id)
            if cell:
                await sheet.delete_row(cell.row)
                return await msg.reply("Удалила!")
        return await msg.reply("Не нашла этого в книге...")


@router.message(F.text)
async def catchall(message: Message):
    return await message.reply(f"Не поняла... \n\n{TIP_TEXT}")
