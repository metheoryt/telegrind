import logging
import time
from datetime import datetime, timedelta, timezone

from gspread import Cell, WorksheetNotFound
from gspread.utils import ValueInputOption, rowcol_to_a1
from gspread_asyncio import AsyncioGspreadSpreadsheet, AsyncioGspreadWorksheet
from pydantic import BaseModel, ValidationError
from pydantic_extra_types.currency_code import Currency

from telegrind.registry import KEY_HEADER

log = logging.getLogger(__name__)

#: Rows to give a newly created worksheet. Creating it with 1 row makes
#: every append grow the grid, and makes "A2:E" an invalid range until
#: something is written — which is how /rebuild first died on Telemetry.
NEW_WORKSHEET_ROWS = 1000

#: (`_config` row label, `Config` field, converter). Hoisted out of
#: `ConfigSheet` so `parse_config` can sit anywhere in this module.
CONFIG_KEYS: list[tuple[str, str, object]] = [
    ("Часовой пояс (в часах)", "dt_offset", int),
    ("Основная валюта", "currency", lambda x: x.strip().upper()),
]


class Config(BaseModel):
    dt_offset: int = 6
    currency: Currency = Currency("KZT")

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.dt_offset))

    def now(self) -> datetime:
        return datetime.now(tz=self.tz)

    def nowstr(self) -> str:
        return self.now().strftime("%d.%m.%y %H:%M")

    def localized(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    @property
    def tzname(self) -> str:
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


class HeaderMismatchError(Exception):
    """The declared columns collide with a column the user owns.

    Every write is positional: `write_rows` puts `len(headers)` values at
    `A<row>`, and `clear_data` empties `data_range(len(headers))`. So the
    declared headers must be a *prefix* of the worksheet's real header row.
    Widen a category by one column in `_categories` and, without this, the
    next projection writes over whatever sits in the column that just
    joined the declared range — on a real workbook that is a formula column
    filled down every row, and `/rebuild` takes the whole column with it.

    Raised instead of writing. The message log is already committed by the
    time projection runs, so the fact is not lost: fix the header (or the
    registry) and `/rebuild` puts it back.
    """


def check_headers(worksheet: str, declared: list[str], actual: list[str]) -> None:
    """Raise unless `declared` is a prefix of `actual`.

    An *empty* cell in `actual` is unclaimed territory and fine to write
    into — that is what makes adding a column to a narrow sheet work. Only
    a differing non-empty header is a collision. A user column with
    formulas but no header cannot be detected from row 1, and is the one
    case this guard misses.
    """
    for index, header in enumerate(declared):
        if index >= len(actual):
            return
        found = actual[index].strip()
        if found and found != header:
            column = rowcol_to_a1(1, index + 1).rstrip("1")
            raise HeaderMismatchError(
                f"{worksheet}!{column}1 holds {found!r}, but the registry "
                f"declares {header!r} there. Nothing was written."
            )


class ConfigSheet:
    """The `_config` worksheet: timezone and default currency.

    Standalone rather than built on `Worksheet`, because `Worksheet.agw()`
    returns only the worksheet and this needs the `created` flag to write
    its defaults exactly once. The two Russian labels and the 2x2 shape are
    already sitting in real spreadsheets and must not change.
    """

    ws_name = "_config"
    ws_dim = (2, 2)
    keys = CONFIG_KEYS

    def __init__(self, ags: AsyncioGspreadSpreadsheet) -> None:
        self.ags = ags
        self._agw: AsyncioGspreadWorksheet | None = None
        self._cfg: Config | None = None

    async def get_agw(self) -> tuple[AsyncioGspreadWorksheet, bool]:
        if self._agw is not None:
            return self._agw, False
        created = False
        try:
            self._agw = await self.ags.worksheet(self.ws_name)
        except WorksheetNotFound:
            self._agw = await self.ags.add_worksheet(
                self.ws_name, rows=self.ws_dim[0], cols=self.ws_dim[1]
            )
            created = True
        if created:
            await self.write_data(Config())
        return self._agw, created

    async def write_data(self, conf: Config) -> None:
        agw, _ = await self.get_agw()
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
                self.name, rows=NEW_WORKSHEET_ROWS, cols=len(self.headers)
            )
            await self._write_header()
        else:
            # Only a *wholly* empty sheet is repaired. Prepending a header to
            # a sheet that already has data would shift every row down and
            # orphan it; reconciling that is the importer's job.
            row = await self._agw.row_values(1)
            if not row:
                await self._write_header()
            else:
                check_headers(self.name, self.headers, row)
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

    async def write_rows(self, rows: list[list[object]], first_row: int = 2) -> None:
        """Write rows at an explicit range starting at `first_row`.

        Deliberately not `append`: `values.append` places rows after the
        sheet's *data extent*, not after the last row of the declared range.
        On a worksheet carrying the user's own formula columns — filled down
        every row — that extent sits far below the range /rebuild just
        cleared, so appending left 3501 expenses starting at row 3505 with
        3503 blank rows above them. Measured on a real workbook, 2026-09-09.
        """
        if not rows:
            return
        agw = await self.agw()
        needed = first_row + len(rows) - 1
        if agw.row_count < needed:
            # Never shrink: the columns past the declared range are the
            # user's, and they run the height of the sheet.
            await agw.resize(rows=needed)
        await agw.update(
            rows,
            range_name=f"A{first_row}",
            value_input_option=ValueInputOption.user_entered,
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
        if agw.row_count <= 1:
            # A grid with only the header row has nothing to clear, and
            # "A2:E" against it is a 400 from the API, not a no-op.
            return
        await agw.batch_clear([data_range(len(self.headers))])

    async def apply_filter(self) -> None:
        # "A:A" specifically, so a user-added column is never captured.
        agw = await self.agw()
        await agw.set_basic_filter("A:A")


_config_cache: dict[str, tuple[float, Config]] = {}
CONFIG_TTL_SECONDS = 60.0


def invalidate_config(cache_key: str | None = None) -> None:
    """Drop the cached config for one chat, or for all of them."""
    if cache_key is None:
        _config_cache.clear()
    else:
        _config_cache.pop(cache_key, None)


async def load_config(
    ags: AsyncioGspreadSpreadsheet | None,
    cache_key: str,
    *,
    now: float | None = None,
) -> Config:
    """Read `_config`, cached for CONFIG_TTL_SECONDS, keyed by chat.

    With no workbook the defaults stand: +06:00 and KZT. Timezone matters
    before any workbook exists — it is what dates a fact — so this cannot
    be an error path.

    Keyed by chat rather than by URL for the same reason as the registry
    cache: `invalidate_config(chat.sheet_url)` with a null URL cleared every
    chat's config.
    """
    if ags is None:
        return Config()

    at = time.monotonic() if now is None else now
    cached = _config_cache.get(cache_key)
    if cached and at - cached[0] < CONFIG_TTL_SECONDS:
        return cached[1]

    cfg = await ConfigSheet(ags).get_data()
    _config_cache[cache_key] = (at, cfg)
    return cfg
