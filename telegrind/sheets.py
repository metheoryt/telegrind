import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Pattern

from aiogram.types import Message
from gspread import WorksheetNotFound, Cell
from gspread.utils import ValueInputOption, rowcol_to_a1
from gspread_asyncio import AsyncioGspreadWorksheet, AsyncioGspreadSpreadsheet


class Sheet:
    ws_name: str
    ws_dim: tuple[int, int]

    def __init__(self, ags: AsyncioGspreadSpreadsheet):
        self.ags = ags
        self._agw = None

    async def get_agw(self) -> (AsyncioGspreadWorksheet, bool):
        if self._agw:
            return self._agw, False
        try:
            self._agw, created = await self.ags.worksheet(self.ws_name), False
        except WorksheetNotFound:
            self._agw, created = await self.ags.add_worksheet(
                self.ws_name,
                rows=self.ws_dim[0],
                cols=self.ws_dim[1]
            ), True
        return self._agw, created


@dataclass
class Config:
    dt_offset: int = 6
    currency: str = 'KZT'

    @property
    def tz(self):
        return timezone(timedelta(hours=self.dt_offset))

    def now(self):
        return datetime.now(tz=self.tz)


class ConfigSheet(Sheet):
    ws_name = '_config'
    ws_dim = (2, 2)

    keys = [
        ('Часовой пояс (в часах)', 'dt_offset', int),
        ('Основная валюта', 'currency', lambda x: x.strip().upper())
    ]

    def __init__(self, ags: AsyncioGspreadSpreadsheet):
        super().__init__(ags)
        self._cfg = None

    async def get_agw(self) -> (AsyncioGspreadWorksheet, bool):
        agw: AsyncioGspreadWorksheet
        agw, created = await super().get_agw()
        if created:
            await self.write_data(Config())
        return agw, created

    async def write_data(self, conf: Config):
        agw: AsyncioGspreadWorksheet
        agw, created = await super().get_agw()
        cells = []
        cells.extend([Cell(i + 1, 1, k[0]) for i, k in enumerate(self.keys)])
        cells.extend([Cell(i + 1, 2, getattr(conf, k[1])) for i, k in enumerate(self.keys)])
        await agw.update_cells(cells, ValueInputOption.user_entered)

    async def get_data(self) -> Config:
        if not self._cfg:
            agw: AsyncioGspreadWorksheet
            agw, _ = await self.get_agw()
            rows = await agw.get_values()
            data = {k[1]: k[2](rows[i][1]) for i, k in enumerate(self.keys)}
            self._cfg = Config(**data)
        return self._cfg


class Transaction(Sheet):
    pattern: Pattern
    headers: list

    async def apply_filter(self, agw: AsyncioGspreadWorksheet):
        r_col = re.sub(r'\d+', '', rowcol_to_a1(1, self.ws_dim[1]))
        await agw.set_basic_filter(f'A:{r_col}')

    async def get_agw(self):
        agw, created = await super().get_agw()
        if created:
            await agw.append_row(self.headers, table_range='A1')
        return agw, created

    @classmethod
    def parse(cls, text: str) -> tuple:
        match = cls.pattern.match(text)
        if not match:
            raise ValueError(f'text does not match pattern of {cls.__name__}')
        return match.groups()

    async def record(self, *args, **kwargs) -> None:
        pass

    async def write_rows(self, rows: list):
        agw: AsyncioGspreadWorksheet
        agw, _ = await self.get_agw()
        await agw.append_rows(
            rows,
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )
        await self.apply_filter(agw)

    async def write_row(self, row: list):
        agw: AsyncioGspreadWorksheet
        agw, _ = await self.get_agw()
        await agw.append_row(
            row,
            value_input_option=ValueInputOption.user_entered,
            table_range='A1'
        )
        await self.apply_filter(agw)


_amount = r'(\d+(?:[\.,]\d+)?)'
_curr = r'([A-z]{3})'
_date = r'(\d{2}\.\d{2}\.\d{4})'


class Outcome(Transaction):
    pattern = re.compile(rf'^{_amount}(?: {_curr})?(?: {_date})?(?: (.*))?$')
    ws_name = 'Expenses'
    headers = ['#', 'Сумма', 'Валюта', 'Дата', 'Комментарий']
    ws_dim = (1, len(headers))

    async def record(self, message: Message, cfg: ConfigSheet) -> None:
        conf = await cfg.get_data()

        amount, curr, date, desc = self.parse(message.text)
        curr = curr or conf.currency
        date = datetime.strptime(date, '%d.%m.%Y') if date else conf.now()
        amount = float(amount.replace(',', '.'))
        await self.write_row([
            message.message_id,
            amount,
            curr,
            date.strftime('%d.%m.%y %H:%M'),
            desc
        ])

    @classmethod
    def from_ticket(cls, message: Message, data: dict) -> list:
        return [
            message.message_id,
            data['ticket']['totalSum'],
            'KZT',
            datetime.fromisoformat(data['ticket']['transactionDate']).strftime('%d.%m.%y %H:%M'),
            'Покупки'
        ]


class Loan(Outcome):
    pattern = re.compile(rf'^(?:долг|за[еёйи]м) (.*?) ([+-])?{_amount}(?: {_curr})?(?: {_date})?(?: (.*))?$', flags=re.I)
    ws_name = 'Loans'
    headers = ['#', "Сумма", "Валюта", "Заёмщик", "Дата", "Комментарий"]
    ws_dim = (1, len(headers))

    async def record(self, message: Message, cfg: ConfigSheet) -> None:
        conf = await cfg.get_data()

        who, direction, amount, curr, date, desc = self.parse(message.text)
        who = who.strip() if who else "Неизвестно"
        direction = -1 if direction in ('-', None) else 1  # -100 and 100 both mean loan, +100 means payback
        amount = float(amount.replace(',', '.')) * direction
        curr = curr or conf.currency
        date = datetime.strptime(date, '%d.%m.%Y') if date else conf.now()
        await self.write_row(
            [message.message_id, amount, curr, who, date.strftime('%d.%m.%y %H:%M'), desc]
        )


class Commodity(Transaction):
    """Commodities list from tiсket."""
    ws_name = 'Commodities'
    ws_dim = (1, 6)
    headers = ['#', "Продукт", "Цена", "Количество", "Дата", "Организация"]

    async def record(self, message: Message, data: dict) -> None:
        org = data['orgTitle']
        t = data['ticket']
        dt = datetime.fromisoformat(t['transactionDate'])
        rows = []
        for i in t['items']:
            cname: str = i['commodity']['name']
            # some tickets have order number in goods name, removing
            cname = re.sub(r'^\d+\.\s+', '', cname)
            row = [
                message.message_id,
                cname,
                i['commodity']['price'],
                i['commodity']['quantity'],
                dt.strftime('%d.%m.%y %H:%M'),
                org
            ]
            rows.append(row)

        await self.write_rows(rows)
