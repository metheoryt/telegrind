from aiogram import flags, F
from aiogram.types import Message
from gspread_asyncio import AsyncioGspreadSpreadsheet, AsyncioGspreadClient

from telegrind.models import Chat
from telegrind.sheets import Outcome, Loan, Wish
from telegrind.bot.router import router
from telegrind.bot.const import TIP_TEXT
from pydantic import BaseModel, PositiveFloat, PositiveInt
from pydantic_extra_types.currency_code import Currency
from datetime import datetime
import marvin
import logging
from aiogram.utils import markdown as md

log = logging.getLogger(__name__)
# marvin.settings.agent_model = "google-gla:gemini-2.5-flash"


class Expense(BaseModel):
    amount: PositiveFloat | PositiveInt
    currency: Currency | None = None
    date: datetime | None = None
    description: str = ""

    @property
    def amount_str(self) -> str:
        return f"{self.amount}".replace(".", ",")

    @property
    def date_str(self) -> str:
        if self.date is None:
            return ""
        return self.date.strftime("%d.%m.%y %H:%M")


log.info("marvin default model is %s", marvin.defaults.model)


@router.message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_outcome_llm(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    is_expense_record = await marvin.run_async(
        "Does the user message contain a number, in digits or in words?",
        context={"user_message": message.text[:200]},
        result_type=bool,
    )
    if not is_expense_record:
        return await message.reply(TIP_TEXT)

    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    expense_sheet = Outcome(ags)
    config = await expense_sheet.cfg.get_data()

    now = config.now()
    extract_instructions = f"""\
How to extract expense date:
    If there is no date and time, assume now.
    If there is only a part of the date or time, or it is relative, count it from now.
    For ambiguous dates, assume they are in the past, and are closest to the present.
    Take into account arbitrary descriptions of date and time, such as midnight, last monday, and so on.
    If there are dates that aren't meant to be expense dates, do not extract them into expense date.
How to extract expense amount:
    Look for a positive integer or decimal number, usually in a beginning of a message.
How to extract expense currency:
    Look for a currency symbol or name, usually following right after expense amount.
    It might be a symbol like $, €, a name like USD, EUR, or a name like тенге, рублей, бат.
    If there is no currency, assume {config.currency}.
How to extract expense description:
    Look for a text after expense amount and currency.
    Description might itself contain a date.
    Exclude extracted expense date, whether absolute or relative, from the description.

Important:
    Do not make things up.
    Do not use as an expense date those dates that are a part of the description.
    Try to extract at least the amount of expense. Put everything that wasn't extracted into the description.

Use next data:
    Current date and time: {now.isoformat()}
"""

    exps = await marvin.extract_async(
        message.text,
        Expense,
        instructions=extract_instructions,
    )
    exp = exps[0]
    exp.date = exp.date or now

    await Outcome(ags).write_row(
        [
            message.message_id,
            message.text,
            exp.amount_str,
            exp.currency,
            exp.date_str,
            exp.description,
        ]
    )

    joke = await marvin.run_async(
        """
Make a funny, ironic remark about your boss' expense, one sentence maximum. Use russian language.
Be creative and smart about it, but also be respectful and humane. Write it as you would say it to your boss personally.
Be like Jarvis for Tony Stark, or like Alfred Pennyworth for Batman.

Focus on the value of the expense, and whether it worths it. Note missing, too short or ambiguous description.
Come up with an interesting fact about the amount number, from history, culture, or science. Note if the time of the expense is somewhat unusual.
Maybe ask a rhetorical question about the expense.
Don't cite the description directly, but you can mention some parts of it.
Avoid references to the boss' gender, you don't know which gender your boss is.
        """,
        context={"boss_message": message.text, "boss_expense": exp},
    )

    return await message.reply(
        f"""\
`Сумма: {exp.amount} {exp.currency}`
`Дата: {exp.date_str}`
`Описание: {exp.description}`

{md.italic(joke)}
""",
        disable_notification=True,
        parse_mode="MarkdownV2",
    )


# @router.message(F.text.regexp(Outcome.pattern))
# @flags.chat_action(action="typing", initial_sleep=0.5)
# async def record_outcome(message: Message, agc: AsyncioGspreadClient, chat: Chat):
#     ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
#     await Outcome(ags).record(message)
#     return await message.reply("Записала!")


# @router.message(F.text.regexp(Loan.pattern))
# @flags.chat_action(action="typing", initial_sleep=0.5)
# async def record_loan(message: Message, agc: AsyncioGspreadClient, chat: Chat):
#     ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
#     await Loan(ags).record(message)
#     return await message.reply("Записала!")


# @router.message(F.text.regexp(Wish.pattern))
# @flags.chat_action(action="typing", initial_sleep=0.5)
# async def record_wish(message: Message, agc: AsyncioGspreadClient, chat: Chat):
#     ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
#     await Wish(ags).record(message)
#     return await message.reply("Записала!")


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


# @router.message(F.text)
# async def catchall(message: Message):
#     return await message.reply(f"Не поняла... \n\n{TIP_TEXT}")
