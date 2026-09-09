import pytest
from gspread import WorksheetNotFound

from telegrind.sheets import (
    Config,
    HeaderMismatchError,
    Worksheet,
    check_headers,
    data_range,
    invalidate_config,
    load_config,
    parse_config,
)


def test_parse_config_reads_both_keys() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "3"], ["Основная валюта", "usd"]])
    assert cfg.dt_offset == 3
    assert cfg.currency == "USD"


def test_parse_config_survives_a_blank_value_column() -> None:
    cfg = parse_config([["Часовой пояс (в часах)"], ["Основная валюта", ""]])
    assert cfg == Config()


def test_parse_config_survives_an_empty_sheet() -> None:
    assert parse_config([]) == Config()


def test_parse_config_survives_a_non_numeric_offset() -> None:
    cfg = parse_config(
        [["Часовой пояс (в часах)", "шесть"], ["Основная валюта", "KZT"]]
    )
    assert cfg.dt_offset == Config().dt_offset


def test_parse_config_survives_an_invalid_currency() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "6"], ["Основная валюта", "XXXXX"]])
    assert cfg.currency == Config().currency


def test_data_range_covers_the_declared_columns_only() -> None:
    assert data_range(5) == "A2:E"
    assert data_range(1) == "A2:A"


def test_data_range_past_column_z() -> None:
    assert data_range(27) == "A2:AA"


def test_parse_config_keeps_the_readable_field_when_another_is_invalid() -> None:
    """A typo in one cell must not silently revert the other to its default."""
    cfg = parse_config([["Часовой пояс (в часах)", "3"], ["Основная валюта", "XXXXX"]])
    assert cfg.dt_offset == 3
    assert cfg.currency == Config().currency


class FakeAgw:
    """Minimal AsyncioGspreadWorksheet stand-in."""

    def __init__(self, values: list[list[str]], row_count: int = 1000) -> None:
        self.values = values
        self.appended: list[list[str]] = []
        self.filtered = False
        self.cleared: list[str] = []
        self.updates: list[tuple[str, list[list[object]]]] = []
        self.resized_to: int | None = None
        self._row_count = row_count

    async def row_values(self, row: int) -> list[str]:
        return self.values[row - 1] if row <= len(self.values) else []

    async def get_values(self) -> list[list[str]]:
        return self.values

    async def append_row(self, row: list[str], table_range: str) -> None:
        self.appended.append(row)
        self.values.insert(0, row)

    async def set_basic_filter(self, rng: str) -> None:
        self.filtered = True

    @property
    def row_count(self) -> int:
        return self._row_count

    async def batch_clear(self, ranges: list[str]) -> None:
        self.cleared.extend(ranges)

    async def update(
        self, values: list[list[object]], range_name: str, value_input_option: object
    ) -> None:
        self.updates.append((range_name, values))

    async def resize(self, rows: int) -> None:
        self.resized_to = rows
        self._row_count = rows

    async def append_rows(
        self, rows: list[list[object]], value_input_option: object, table_range: str
    ) -> None:
        self.appended.extend(rows)


class FakeAgs:
    """Minimal AsyncioGspreadSpreadsheet stand-in."""

    def __init__(self, existing: dict[str, FakeAgw]) -> None:
        self.existing = existing
        self.created: list[str] = []
        self.created_rows: dict[str, int] = {}

    async def worksheet(self, name: str) -> FakeAgw:
        if name not in self.existing:
            raise WorksheetNotFound(name)
        return self.existing[name]

    async def add_worksheet(self, name: str, rows: int, cols: int) -> FakeAgw:
        self.created.append(name)
        self.created_rows[name] = rows
        agw = FakeAgw([], row_count=rows)
        self.existing[name] = agw
        return agw


HEADERS = ["#", "Сумма", "Дата"]


async def test_a_created_worksheet_gets_its_header_row() -> None:
    ags = FakeAgs({})
    ws = Worksheet(ags, "Expenses", HEADERS)
    agw = await ws.agw()
    assert ags.created == ["Expenses"]
    assert agw.appended == [HEADERS]


async def test_an_existing_but_empty_worksheet_gets_its_header_row() -> None:
    """agw() writes headers only on create. A hand-made empty Telemetry sheet
    is *found*, so without this guard append() drops a fact into row 1 —
    where keys() reads it as a key and data_range never clears it."""
    agw = FakeAgw([])
    ws = Worksheet(FakeAgs({"Telemetry": agw}), "Telemetry", HEADERS)
    await ws.agw()
    assert agw.appended == [HEADERS]


async def test_a_populated_worksheet_is_left_alone() -> None:
    agw = FakeAgw([HEADERS, ["1_1", "100", "09.09.26 21:40"]])
    ws = Worksheet(FakeAgs({"Expenses": agw}), "Expenses", HEADERS)
    await ws.agw()
    assert agw.appended == []


async def test_a_worksheet_with_data_but_no_header_is_refused_not_repaired() -> None:
    """Repairing row 1 here would push a real data row down and orphan it, so
    it is still never repaired — but proceeding is not safe either. Row 1 is
    what `keys()` skips and what the importer maps columns by, so a data row
    sitting there means the bot's whole layout model is wrong for this sheet.
    Refuse and say so."""
    agw = FakeAgw([["1_1", "100", "09.09.26 21:40"]])
    ws = Worksheet(FakeAgs({"Expenses": agw}), "Expenses", HEADERS)
    with pytest.raises(HeaderMismatchError):
        await ws.agw()
    assert agw.appended == []


async def test_agw_is_cached_so_the_header_probe_happens_once() -> None:
    agw = FakeAgw([HEADERS])
    ws = Worksheet(FakeAgs({"Expenses": agw}), "Expenses", HEADERS)
    assert await ws.agw() is await ws.agw()


def test_pytest_import_is_used() -> None:
    assert pytest is not None


async def test_clear_data_skips_a_grid_with_only_a_header_row() -> None:
    """batch_clear("A2:E") against a 1-row grid is a 400 from the API, not a
    no-op. A freshly created Telemetry sheet took /rebuild down this way."""
    agw = FakeAgw([HEADERS], row_count=1)
    ws = Worksheet(FakeAgs({"Telemetry": agw}), "Telemetry", HEADERS)
    await ws.clear_data()
    assert agw.cleared == []


async def test_clear_data_clears_the_declared_range_on_a_real_grid() -> None:
    agw = FakeAgw([HEADERS], row_count=1000)
    ws = Worksheet(FakeAgs({"Expenses": agw}), "Expenses", HEADERS)
    await ws.clear_data()
    assert agw.cleared == ["A2:C"]


async def test_a_created_worksheet_gets_a_usable_grid() -> None:
    """rows=1 would make every append grow the grid and "A2:E" invalid."""
    ags = FakeAgs({})
    await Worksheet(ags, "Telemetry", HEADERS).agw()
    assert ags.created_rows["Telemetry"] > 1


def _ws(agw: FakeAgw) -> Worksheet:
    return Worksheet(FakeAgs({"Expenses": agw}), "Expenses", HEADERS)


async def test_write_rows_writes_at_a2_and_never_appends() -> None:
    """append places rows after the sheet's data extent, which the user's own
    formula columns push to the bottom of the sheet. On a real workbook that
    put 3501 rebuilt expenses at row 3505 under 3503 blank rows."""
    agw = FakeAgw([HEADERS], row_count=1000)
    await _ws(agw).write_rows([["1_1", 1, "x"], ["1_2", 2, "y"]])
    assert agw.updates == [("A2", [["1_1", 1, "x"], ["1_2", 2, "y"]])]
    assert agw.appended == []


async def test_write_rows_grows_the_grid_when_the_rows_do_not_fit() -> None:
    agw = FakeAgw([HEADERS], row_count=10)
    await _ws(agw).write_rows([[f"1_{i}", i, ""] for i in range(50)])
    assert agw.resized_to == 51


async def test_write_rows_never_shrinks_the_grid() -> None:
    """Columns past the declared range are the user's and run the sheet's
    height; shrinking would delete them."""
    agw = FakeAgw([HEADERS], row_count=5000)
    await _ws(agw).write_rows([["1_1", 1, "x"]])
    assert agw.resized_to is None


async def test_write_rows_of_nothing_touches_nothing() -> None:
    agw = FakeAgw([HEADERS], row_count=1000)
    await _ws(agw).write_rows([])
    assert agw.updates == []
    assert agw.resized_to is None


def test_check_headers_accepts_an_exact_match() -> None:
    check_headers("Expenses", ["#", "Сумма"], ["#", "Сумма"])


def test_check_headers_accepts_the_users_extra_columns() -> None:
    """Declared headers only need to be a *prefix*. F onward is his."""
    check_headers("Expenses", ["#", "Сумма"], ["#", "Сумма", "Курс", "В тенге"])


def test_check_headers_accepts_a_narrower_sheet() -> None:
    """Empty territory to the right is fine to write into."""
    check_headers("Expenses", ["#", "Сумма", "Валюта"], ["#", "Сумма"])


def test_check_headers_accepts_a_blank_cell_inside_the_declared_range() -> None:
    check_headers("Expenses", ["#", "Сумма", "Валюта"], ["#", "", "Валюта"])


def test_check_headers_refuses_a_collision_with_a_user_column() -> None:
    """Widening a category by one column must not eat the column it lands on.

    This is the whole point of the guard: `write_rows` is positional, so
    declaring a 3rd column on a sheet whose 3rd column is the user's `Курс`
    would overwrite it on every write and blank it on /rebuild.
    """
    with pytest.raises(HeaderMismatchError) as exc:
        check_headers(
            "Expenses", ["#", "Сумма", "Валюта"], ["#", "Сумма", "Курс", "В тенге"]
        )
    assert "Expenses!C1" in str(exc.value)
    assert "Курс" in str(exc.value)
    assert "Валюта" in str(exc.value)


async def test_agw_refuses_a_worksheet_whose_headers_collide() -> None:
    agw = FakeAgw([["#", "Сумма", "Курс"]])
    ws = Worksheet(FakeAgs({"Expenses": agw}), "Expenses", ["#", "Сумма", "Валюта"])
    with pytest.raises(HeaderMismatchError):
        await ws.agw()
    assert agw.updates == []
    assert agw.cleared == []


async def test_load_config_with_no_workbook_returns_the_defaults() -> None:
    """Timezone is what dates a fact, so it cannot come only from a sheet.

    With nothing linked the defaults stand — +06:00 and KZT — which is what
    lets `Дата` resolve correctly on the very first message.
    """
    invalidate_config()
    cfg = await load_config(None, "chat-1")
    assert cfg.dt_offset == 6
    assert str(cfg.currency) == "KZT"
