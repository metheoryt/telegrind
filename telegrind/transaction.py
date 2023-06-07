import re
from datetime import datetime
from typing import Pattern

from gspread import WorksheetNotFound
from gspread.utils import ValueInputOption, rowcol_to_a1
from gspread_asyncio import AsyncioGspreadWorksheet, AsyncioGspreadSpreadsheet


class Transaction:
    pattern: Pattern
    ws_name: str
    headers: list
    default_currency: str = 'KZT'

    @classmethod
    async def get_ws(cls, ags: AsyncioGspreadSpreadsheet):
        try:
            return await ags.worksheet(cls.ws_name)
        except WorksheetNotFound:
            agw = await ags.add_worksheet(cls.ws_name, rows=100, cols=100)
            await agw.append_row(cls.headers, table_range='A1')
            r_col = re.sub(r'\d+', '', rowcol_to_a1(1, len(cls.headers)))
            await agw.set_basic_filter(f'A:{r_col}')
            return agw

    @classmethod
    def parse(cls, text: str) -> tuple:
        match = cls.pattern.match(text)
        if not match:
            raise ValueError(f'text does not match pattern of {cls.__name__}')
        return match.groups()

    @classmethod
    async def record(cls, agw: AsyncioGspreadWorksheet, data: tuple) -> None:
        pass


_amount = r'(\d+(?:[\.,]\d+)?)'
_curr = r'([A-z]{3})'
_date = r'(\d{2}\.\d{2}\.\d{4})'


class Conversion(Transaction):
    pattern = re.compile(rf'^{_amount}\s+{_curr}\s+>\s+{_amount}\s+{_curr}(?:\s+(.*))?$')
    ws_name = 'Base'
    headers = ['#', 'Сумма', 'Назначение', 'Валюта', 'Тип', 'Дата']

    @classmethod
    async def record(cls, ags: AsyncioGspreadSpreadsheet, data: tuple) -> None:
        agw = await cls.get_ws(ags)
        mid, fa, fc, ta, tc, desc = data
        # desc = desc or ''
        fc, tc = fc.upper(), tc.upper()
        fa, ta = float(fa.replace(',', '.')), float(ta.replace(',', '.'))
        dt = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        await agw.append_rows(
            [
                [mid, fa, desc, fc, f'конвертация в {tc}', dt],
                [mid, ta, desc, tc, f'конвертация из {fc}', dt]
            ],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )


class Income(Transaction):
    pattern = re.compile(rf'^\+{_amount}(?: {_curr})?(?: (.*))?$')
    ws_name = 'Base'
    headers = ['#', 'Сумма', 'Назначение', 'Валюта', 'Тип', 'Дата']

    @classmethod
    async def record(cls, ags: AsyncioGspreadSpreadsheet, data: tuple) -> None:
        agw = await cls.get_ws(ags)
        mid, amount, curr, desc = data
        curr = curr or cls.default_currency
        # desc = desc or ''
        amount = float(amount.replace(',', '.'))
        await agw.append_row(
            [mid, amount, desc, curr, 'доход', datetime.now().strftime('%d.%m.%Y %H:%M:%S')],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )


class Outcome(Transaction):
    pattern = re.compile(rf'^{_amount}(?: {_curr})?(?: {_date})?(?: (.*))?$')
    ws_name = 'Base'
    headers = ['#', 'Сумма', 'Назначение', 'Валюта', 'Тип', 'Дата']

    @classmethod
    async def record(cls, ags: AsyncioGspreadSpreadsheet, data: tuple) -> None:
        agw = await cls.get_ws(ags)
        mid, amount, curr, date, desc = data
        curr = curr or cls.default_currency
        # desc = desc or ''
        date = datetime.strptime(date, '%d.%m.%Y') if date else datetime.now()
        amount = float(amount.replace(',', '.'))
        await agw.append_row(
            [mid, amount, desc, curr, 'расход', date.strftime('%d.%m.%Y %H:%M:%S')],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )


class Loan(Outcome):
    pattern = re.compile(rf'^(?:долг|за[еёйи]м) (.*?) ([+-])?{_amount}(?: {_curr})?(?: {_date})?(?: (.*))?$', flags=re.I)
    ws_name = 'Loans'
    headers = ['#', "Сумма", "Валюта", "Заёмщик", "Дата", "Комментарий"]

    @classmethod
    async def record(cls, ags: AsyncioGspreadSpreadsheet, data: tuple) -> None:
        agw = await cls.get_ws(ags)
        mid, who, direction, amount, curr, date, desc = data
        who = who.strip() if who else "Неизвестно"
        direction = -1 if direction in ('-', None) else 1  # -100 and 100 both mean loan, +100 means payback
        amount = float(amount.replace(',', '.')) * direction
        curr = curr or cls.default_currency
        date = datetime.strptime(date, '%d.%m.%Y') if date else datetime.now()
        desc = desc.strip() if desc else ""

        await agw.append_row(
            [mid, amount, curr, who, date.strftime('%d.%m.%Y %H:%M:%S'), desc],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )
