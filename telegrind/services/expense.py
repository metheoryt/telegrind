from datetime import datetime
from gspread_asyncio import AsyncioGspreadSpreadsheet
import marvin
from pydantic import BaseModel, PositiveFloat, PositiveInt
from pydantic_extra_types.currency_code import Currency
from telegrind.sheets import Outcome
from aiogram.types import Message


EXTRACT_INSTRUCTION = """\
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
    If there is no currency, assume {default_currency}.
How to extract expense description:
    Look for a text after expense amount and currency.
    Description might itself contain a date.
    Exclude extracted expense date, whether absolute or relative, from the description.

Important:
    - Do not make things up.
    - Do not use as an expense date those dates that are a part of the description.
    - Try to extract at least the amount of expense. Put everything that wasn't extracted into the description.

Use next data:
    Current date and time: {now}
"""

JOKE_REMARK_INSTRUCTION = """
Make a funny, ironic remark/phrase/quote about your boss' expense. Use russian language. Keep in mind that you are saying it personally to the boss.
Be like Alfred Pennyworth for Bruce Wayne - creative and witty, but also respectful, kind and humane.

You can come up with an interesting fact about the amount number, from history, culture, or science.
Or you can ask a rhetorical question about the expense, if it is appropriate.

Note missing, too short or ambiguous description. Note if the time of the expense is somewhat unusual.

Important:
    - Avoid quoting expense data directly, but you can mention it partly or indirectly.
    - Avoid references to the boss' gender, do not use pronouns and do not appeal to the boss.
    - Use proper nouns as is, without translation.
    - Do not make things up, do not compare the amount to any other expenses.
    - Write one short sentence at most!
"""


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


class ExpenseService:
    def __init__(self, wb: AsyncioGspreadSpreadsheet) -> None:
        self.wb: AsyncioGspreadSpreadsheet = wb
        self.ws: Outcome = Outcome(self.wb)

    @classmethod
    async def is_expense(cls, msg_text: str):
        is_expense_record = await marvin.run_async(
            "Does the user message contain a number, in digits or in words?",
            context={"user_message": msg_text[:200]},
            result_type=bool,
        )
        return is_expense_record

    async def extract_expense(self, msg_text: str):
        config = await self.ws.cfg.get_data()
        exps = await marvin.extract_async(
            data=msg_text,
            target=Expense,
            instructions=EXTRACT_INSTRUCTION.format(
                default_currency=config.currency, now=config.now().isoformat()
            ),
        )
        exp = exps[0]
        exp.date = exp.date or config.now()
        return exp

    async def joke_remark(self, msg_text: str, exp: Expense):
        remark = await marvin.run_async(
            JOKE_REMARK_INSTRUCTION,
            context={"boss_message": msg_text, "boss_expense": exp},
        )
        return remark

    async def make_reply_text(self, message: Message, expense: Expense):
        remark = await self.joke_remark(message.text, expense)
        reply = f"""\
<code>{expense.amount} {expense.currency.lower()} {expense.date.strftime("%d.%m.%y в %H:%M")} "{expense.description}"</code>

<i>{remark}</i>

<tg-spoiler>{message.message_id}@{self.ws.ws_name}</tg-spoiler>
"""
        return reply

    @classmethod
    def get_row(cls, message: Message, expense: Expense) -> list[str]:
        return [
            message.message_id,
            expense.amount_str,
            expense.currency,
            expense.date_str,
            expense.description,
        ]

    async def add_expense(self, message: Message, expense: Expense) -> None:
        row = self.get_row(message, expense)
        await self.ws.write_row(row)

    async def expense_exists(self, message: Message) -> bool:
        cell = await self.ws.search_row(message.message_id)
        return cell is not None

    async def update_expense(self, message: Message, new_expense: Expense) -> bool:
        cell = await self.ws.search_row(message.message_id)
        if not cell:
            return False

        row = self.get_row(message, new_expense)
        await self.ws.change_row(cell.row, row)
        return True
