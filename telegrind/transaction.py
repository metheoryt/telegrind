import re
from datetime import datetime
from typing import Pattern

from gspread.utils import ValueInputOption
from gspread_asyncio import AsyncioGspreadWorksheet


class Transaction:
    pattern: Pattern

    @classmethod
    def parse(cls, text: str) -> tuple:
        match = cls.pattern.match(text)
        if not match:
            raise ValueError(f'text does not match pattern of {cls.__name__}')
        return match.groups()

    @classmethod
    async def record(cls, agw: AsyncioGspreadWorksheet, data: tuple):
        aa = await agw.get_values('A:A')
        next_row = len(aa) + 1
        return await cls._record(agw, data, next_row)

    @classmethod
    async def _record(cls, agw: AsyncioGspreadWorksheet, data: tuple, next_row: int) -> None:
        pass


_amount = r'(\d+(?:[\.,]\d+)?)'
_curr = r'([A-z]{3})'


class Conversion(Transaction):
    pattern = re.compile(rf'^{_amount}\s+{_curr}\s+>\s+{_amount}\s+{_curr}(?:\s+(.*))?$')

    @classmethod
    async def _record(cls, agw: AsyncioGspreadWorksheet, data: tuple, next_row: int) -> None:
        fa, fc, ta, tc, desc = data
        # desc = desc or ''
        fc, tc = fc.upper(), tc.upper()
        fa, ta = float(fa.replace(',', '.')), float(ta.replace(',', '.'))
        dt = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        await agw.append_rows(
            [
                [fa, desc, fc, f'конвертация в {tc}', dt],
                [ta, desc, tc, f'конвертация из {fc}', dt]
            ],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )
        # await agw.update_cells([
        #     # outcome transaction
        #     Cell(next_row, 1, fa),
        #     Cell(next_row, 2, desc),
        #     Cell(next_row, 3, fc),
        #     Cell(next_row, 4, f'конвертация в {tc}'),
        #     Cell(next_row, 5, dt),
        #     # income transaction
        #     Cell(next_row + 1, 1, ta),
        #     Cell(next_row + 1, 2, desc),
        #     Cell(next_row + 1, 3, tc.upper()),
        #     Cell(next_row + 1, 4, f'конвертация из {fc}'),
        #     Cell(next_row + 1, 5, dt),
        # ])


class Income(Transaction):
    pattern = re.compile(rf'^\+{_amount}(?: {_curr})?(?: (.*))?$')
    default_currency = 'KZT'

    @classmethod
    async def _record(cls, agw: AsyncioGspreadWorksheet, data: tuple, next_row: int) -> None:
        amount, curr, desc = data
        curr = curr or cls.default_currency
        # desc = desc or ''
        amount = float(amount.replace(',', '.'))
        await agw.append_row(
            [amount, desc, curr, 'доход', datetime.now().strftime('%d.%m.%Y %H:%M:%S')],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )
        # await agw.update_cells([
        #     Cell(next_row, 1, amount),
        #     Cell(next_row, 2, desc),
        #     Cell(next_row, 3, curr),
        #     Cell(next_row, 4, 'доход'),
        #     Cell(next_row, 5, datetime.now().strftime('%d.%m.%Y %H:%M:%S')),
        # ])


class Outcome(Transaction):
    pattern = re.compile(rf'^{_amount}(?: {_curr})?(?: (.*))?$')
    default_currency = 'KZT'

    @classmethod
    async def _record(cls, agw: AsyncioGspreadWorksheet, data: tuple, next_row: int) -> None:
        amount, curr, desc = data
        curr = curr or cls.default_currency
        # desc = desc or ''
        amount = float(amount.replace(',', '.'))
        await agw.append_row(
            [amount, desc, curr, 'расход', datetime.now().strftime('%d.%m.%Y %H:%M:%S')],
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )
        # amount = float(amount.replace(',', '.'))
        # await agw.update_cells([
        #     Cell(next_row, 1, amount),
        #     Cell(next_row, 2, desc),
        #     Cell(next_row, 3, curr),
        #     Cell(next_row, 4, 'расход'),
        #     Cell(next_row, 5, datetime.now().strftime('%d.%m.%Y %H:%M:%S')),
        # ])
