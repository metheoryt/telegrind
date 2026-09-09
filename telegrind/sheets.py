import logging
import re
import time
from datetime import datetime, timedelta, timezone
from re import Pattern

from aiogram.types import Message
from dateparser.search import search_dates
from gspread import Cell, WorksheetNotFound
from gspread.utils import ValueInputOption, rowcol_to_a1
from gspread_asyncio import AsyncioGspreadSpreadsheet, AsyncioGspreadWorksheet
from pydantic import BaseModel, ValidationError
from pydantic_extra_types.currency_code import Currency

from telegrind.registry import KEY_HEADER

log = logging.getLogger(__name__)

#: (`_config` row label, `Config` field, converter). Hoisted out of
#: `ConfigSheet` so `parse_config` can sit anywhere in this module.
CONFIG_KEYS: list[tuple[str, str, object]] = [
    ("Часовой пояс (в часах)", "dt_offset", int),
    ("Основная валюта", "currency", lambda x: x.strip().upper()),
]


class Sheet:
    ws_name: str
    ws_dim: tuple[int, int]

    def __init__(self, ags: AsyncioGspreadSpreadsheet):
        self.ags = ags
        self._agw = None

    async def get_agw(self) -> tuple[AsyncioGspreadWorksheet, bool]:
        if self._agw:
            return self._agw, False
        try:
            self._agw, created = await self.ags.worksheet(self.ws_name), False
        except WorksheetNotFound:
            self._agw, created = (
                await self.ags.add_worksheet(
                    self.ws_name, rows=self.ws_dim[0], cols=self.ws_dim[1]
                ),
                True,
            )
        return self._agw, created


class Config(BaseModel):
    dt_offset: int = 6
    currency: Currency = Currency("KZT")

    @property
    def tz(self):
        return timezone(timedelta(hours=self.dt_offset))

    def now(self) -> datetime:
        return datetime.now(tz=self.tz)

    def nowstr(self) -> str:
        return self.now().strftime("%d.%m.%y %H:%M")

    def localized(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    @property
    def tzname(self):
        """Return timezone in +0600 format."""
        return self.now().strftime("%z")


def parse_config(rows: list[list[str]]) -> Config:
    """Parse the `_config` worksheet. Any unreadable cell falls back to a default.

    `_config` is small and bot-created, but it is still a spreadsheet a user
    can edit, and an IndexError here costs them a message.
    """
    values: dict[str, str] = {}
    for row in rows:
        if len(row) >= 2 and row[0]:
            values[row[0].strip()] = row[1].strip()

    data: dict[str, object] = {}
    for label, field, converter in CONFIG_KEYS:
        raw = values.get(label)
        if not raw:
            continue
        try:
            data[field] = converter(raw)
        except ValueError, TypeError:
            log.warning("_config: cannot read %s from %r, using default", field, raw)

    try:
        return Config(**data)
    except ValidationError:
        pass

    # One bad cell must not revert the readable ones: drop the offending
    # fields individually rather than falling back to an all-defaults Config.
    kept: dict[str, object] = {}
    for field, value in data.items():
        try:
            Config(**kept, **{field: value})
        except ValidationError:
            log.warning("_config: %s=%r failed validation, using default", field, value)
        else:
            kept[field] = value
    return Config(**kept)


def data_range(ncols: int, first_row: int = 2) -> str:
    """`"A2:E"` — the declared column range, unbounded downward.

    Used by /rebuild so that clearing a category's data never reaches a
    column the user added themselves.
    """
    last = rowcol_to_a1(1, ncols).rstrip("1")
    return f"A{first_row}:{last}"


class ConfigSheet(Sheet):
    ws_name = "_config"
    ws_dim = (2, 2)

    keys = CONFIG_KEYS

    def __init__(self, ags: AsyncioGspreadSpreadsheet):
        super().__init__(ags)
        self._cfg = None

    async def get_agw(self) -> tuple[AsyncioGspreadWorksheet, bool]:
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
        cells.extend(
            [Cell(i + 1, 2, getattr(conf, k[1])) for i, k in enumerate(self.keys)]
        )
        await agw.update_cells(cells, ValueInputOption.user_entered)

    async def get_data(self) -> Config:
        if not self._cfg:
            agw, _ = await self.get_agw()
            self._cfg = parse_config(await agw.get_values())
        return self._cfg


class Transaction(Sheet):
    # TODO lock the sheet while updating. We wight want to use redis lock for that.
    pattern: Pattern
    headers: list

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cfg: ConfigSheet = ConfigSheet(self.ags)

    async def apply_filter(self, agw: AsyncioGspreadWorksheet):
        # make it take whole table space,
        # so we don't mess with user-added columns
        await agw.set_basic_filter("A:A")

    async def get_agw(self) -> tuple[AsyncioGspreadWorksheet, bool]:
        agw, created = await super().get_agw()
        if created:
            await agw.append_row(self.headers, table_range="A1")
        return agw, created

    @classmethod
    def parse(cls, text: str) -> tuple:
        match = cls.pattern.match(text)
        if not match:
            raise ValueError(f"text does not match pattern of {cls.__name__}")
        return match.groups()

    async def make_row(self, message: Message) -> list:
        pass

    async def record(self, *args, **kwargs) -> None:
        pass

    async def write_rows(self, rows: list):
        agw, _ = await self.get_agw()
        await agw.append_rows(
            rows, value_input_option=ValueInputOption.user_entered, table_range="A1"
        )
        await self.apply_filter(agw)

    async def write_row(self, row: list[str]) -> None:
        agw, _ = await self.get_agw()
        await agw.append_row(
            row, value_input_option=ValueInputOption.user_entered, table_range="A1"
        )
        # await self.apply_filter(agw)

    async def search_row(self, message_id: int) -> Cell | None:
        agw, _ = await self.get_agw()
        return await agw.find(str(message_id), in_column=1)

    async def change_row(self, row_id: int, row: list):
        agw, _ = await self.get_agw()
        cells = [Cell(row=row_id, col=i + 1, value=v) for i, v in enumerate(row)]
        await agw.update_cells(cells, value_input_option=ValueInputOption.user_entered)
        await self.apply_filter(agw)

    async def delete_row(self, row_id: int):
        agw, _ = await self.get_agw()
        return await agw.delete_rows(row_id)


_amount = r"(\d+(?:[\.,]\d+)?)"
_curr = r"([A-Za-z]{3})"
_date = r"(\d{2}\.\d{2}\.\d{4})"


class Outcome(Transaction):
    pattern = re.compile(rf"^{_amount}\b")
    ws_name = "Expenses"
    headers = ["#", "Сумма", "Валюта", "Дата", "Комментарий"]
    ws_dim = (1, len(headers))

    async def make_row(self, message: Message) -> list:
        conf: Config = await self.cfg.get_data()
        text = message.text

        # amount is mandatory
        amount = re.search(rf"^{_amount}\b", text).group()
        # extracting amount from text
        text = re.sub(rf"^{_amount}\b", "", text).strip()
        amount = float(amount.replace(",", "."))

        # currency is optional
        curr = conf.currency.upper()
        match = re.search(rf"^{_curr}\b", text)
        if match:
            # no currency
            curr = match.group()
            text = re.sub(rf"^{_curr}\b", "", text).strip()
            curr = curr.upper()

        # date is optional
        date = conf.now()
        matches = search_dates(
            text,
            languages=["ru", "en"],
            settings={"TIMEZONE": conf.tzname, "RETURN_AS_TIMEZONE_AWARE": True},
        )
        if matches:
            # take first found date
            sub, date = matches[0]
            text = text.replace(sub, "", 1).strip()
        desc = text

        return [message.message_id, amount, curr, date.strftime("%d.%m.%y %H:%M"), desc]

    async def record(self, *args, **kwargs) -> None:
        row = await self.make_row(*args, **kwargs)
        return await self.write_row(row)


class Loan(Outcome):
    pattern = re.compile(
        rf"^(?:долг|за[еёйи]м) (.*?) ([+-])?{_amount}(?: {_curr})?(?: {_date})?(?: (.*))?$",
        flags=re.I,
    )
    ws_name = "Loans"
    headers = ["#", "Сумма", "Валюта", "Заёмщик", "Дата", "Комментарий"]
    ws_dim = (1, len(headers))

    async def make_row(self, message: Message) -> list:
        conf: Config = await self.cfg.get_data()

        who, direction, amount, curr, date, desc = self.parse(message.text)
        who = who.strip() if who else "Неизвестно"
        direction = (
            -1 if direction in ("-", None) else 1
        )  # -100 and 100 both mean loan, +100 means payback
        amount = float(amount.replace(",", ".")) * direction
        curr = curr or conf.currency
        date = datetime.strptime(date, "%d.%m.%Y") if date else conf.now()
        return [
            message.message_id,
            amount,
            curr,
            who,
            date.strftime("%d.%m.%y %H:%M"),
            desc,
        ]


class Wish(Transaction):
    ws_name = "Wishlist"
    ws_dim = (1, 4)
    headers = ["#", "Желание", "Добавлено", "Исполнено"]
    pattern = re.compile(r"^хочу\s+(.*?)$", re.IGNORECASE)

    async def make_row(self, message: Message) -> list:
        (wish,) = self.parse(message.text)
        conf: Config = await self.cfg.get_data()
        return [message.message_id, wish, conf.nowstr(), ""]

    async def record(self, *args, **kwargs) -> None:
        row = await self.make_row(*args, **kwargs)
        return await self.write_row(row)


class Worksheet:
    """A single worksheet, addressed by name, with a key column in A.

    Replaces the `Sheet` -> `Transaction` -> `Outcome`/`Loan`/`Wish`
    hierarchy: a category is a row of registry data now, so there is
    nothing left for a subclass to express.
    """

    def __init__(
        self, ags: AsyncioGspreadSpreadsheet, name: str, headers: list[str]
    ) -> None:
        self.ags = ags
        self.name = name
        self.headers = headers
        self._agw: AsyncioGspreadWorksheet | None = None

    async def agw(self) -> AsyncioGspreadWorksheet:
        """Get or lazily create the worksheet, ensuring row 1 is the header.

        Everything downstream reads row 1 as the header: `keys()` skips it,
        `data_range` clears from A2, and the importer maps columns by it. A
        worksheet the user made by hand exists but is empty, so it is
        *found* rather than created — the guard below covers that path too.
        Costs one extra read, once per instance, thanks to the `_agw` cache.
        """
        if self._agw is not None:
            return self._agw
        try:
            self._agw = await self.ags.worksheet(self.name)
        except WorksheetNotFound:
            self._agw = await self.ags.add_worksheet(
                self.name, rows=1, cols=len(self.headers)
            )
            await self._write_header()
        else:
            # Only a *wholly* empty sheet is repaired. Prepending a header to
            # a sheet that already has data would shift every row down and
            # orphan it; reconciling that is the importer's job.
            if not await self._agw.row_values(1):
                await self._write_header()
        return self._agw

    async def _write_header(self) -> None:
        assert self._agw is not None
        await self._agw.append_row(self.headers, table_range="A1")
        await self.apply_filter()

    async def all_values(self) -> list[list[str]]:
        agw = await self.agw()
        return await agw.get_values()

    async def append(self, rows: list[list[object]]) -> None:
        if not rows:
            return
        agw = await self.agw()
        await agw.append_rows(
            rows,
            value_input_option=ValueInputOption.user_entered,
            table_range="A1",
        )

    async def keys(self) -> dict[str, int]:
        """Map every key in column A to its 1-based row number."""
        agw = await self.agw()
        column = await agw.col_values(1)
        return {
            value: number
            for number, value in enumerate(column, start=1)
            if value and value != KEY_HEADER
        }

    async def find_key(self, key: str) -> int | None:
        agw = await self.agw()
        cell = await agw.find(str(key), in_column=1)
        return cell.row if cell else None

    async def update_row(self, row_no: int, row: list[object]) -> None:
        agw = await self.agw()
        await agw.update(
            [row],
            range_name=f"A{row_no}",
            value_input_option=ValueInputOption.user_entered,
        )

    async def delete_row(self, row_no: int) -> None:
        agw = await self.agw()
        await agw.delete_rows(row_no)

    async def clear_data(self) -> None:
        """Clear the declared column range below the header row.

        Never `clear()`: user-added columns outside the declared range are
        theirs, and the projection is one-way by design.
        """
        agw = await self.agw()
        await agw.batch_clear([data_range(len(self.headers))])

    async def apply_filter(self) -> None:
        # "A:A" specifically, so a user-added column is never captured.
        agw = await self.agw()
        await agw.set_basic_filter("A:A")


_config_cache: dict[str, tuple[float, Config]] = {}
CONFIG_TTL_SECONDS = 60.0


def invalidate_config(sheet_url: str | None = None) -> None:
    """Drop the cached config for one workbook, or for all of them."""
    if sheet_url is None:
        _config_cache.clear()
    else:
        _config_cache.pop(sheet_url, None)


async def load_config(
    ags: AsyncioGspreadSpreadsheet, sheet_url: str, *, now: float | None = None
) -> Config:
    """Read `_config`, cached for CONFIG_TTL_SECONDS, keyed by sheet_url.

    Today's code builds a fresh ConfigSheet per Transaction, so the
    three-sheet edit loop does three separate _config reads for one message.
    """
    at = time.monotonic() if now is None else now
    cached = _config_cache.get(sheet_url)
    if cached and at - cached[0] < CONFIG_TTL_SECONDS:
        return cached[1]

    cfg = await ConfigSheet(ags).get_data()
    _config_cache[sheet_url] = (at, cfg)
    return cfg
