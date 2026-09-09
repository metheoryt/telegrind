"""The category registry: a user-editable `_categories` worksheet.

Every cell here is untrusted input. A malformed row is collected as a
validation error and dropped; it must never cost the user a message.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gspread_asyncio import AsyncioGspreadSpreadsheet

    from telegrind.sheets import Worksheet

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0

FIELD_TYPES: frozenset[str] = frozenset(
    {"text", "number", "money", "currency", "date", "due"}
)
DEFAULT_FIELD_TYPE = "text"

#: Rollup templates. The names are fixed now so the registry can validate
#: against them; the formulas themselves arrive in Phase 2 (`rollups.py`).
ROLLUP_TEMPLATES: frozenset[str] = frozenset({"balance", "sum_by_period"})

#: Column A of every category worksheet holds the row key, never a field.
KEY_HEADER = "#"

REGISTRY_WORKSHEET = "_categories"
REGISTRY_HEADERS = ["name", "worksheet", "when to use", "columns", "rollup"]

_ROLLUP_RE = re.compile(r"^\s*(\w+)\s*\((.*)\)\s*$")


@dataclass(frozen=True, slots=True)
class Column:
    header: str
    type: str = DEFAULT_FIELD_TYPE


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    worksheet: str
    when_to_use: str
    columns: tuple[Column, ...]
    rollup: str | None = None

    @property
    def headers(self) -> list[str]:
        """The worksheet's header row, key column included."""
        return [KEY_HEADER, *(c.header for c in self.columns)]

    def column(self, header: str) -> Column | None:
        for c in self.columns:
            if c.header == header:
                return c
        return None


@dataclass(frozen=True, slots=True)
class Registry:
    categories: tuple[Category, ...]
    errors: tuple[str, ...] = ()

    def by_name(self, name: str) -> Category | None:
        for c in self.categories:
            if c.name == name:
                return c
        return None

    def by_worksheet(self, worksheet: str) -> Category | None:
        for c in self.categories:
            if c.worksheet == worksheet:
                return c
        return None


FACTS_FALLBACK = Category(
    name="facts",
    worksheet="Facts",
    when_to_use="всё остальное — сохранить как есть",
    columns=(Column("Текст", "text"), Column("Дата", "date")),
)

SEED_CATEGORIES: tuple[Category, ...] = (
    Category(
        name="expense",
        worksheet="Expenses",
        when_to_use="потраченная сумма",
        columns=(
            Column("Сумма", "money"),
            Column("Валюта", "currency"),
            Column("Дата", "date"),
            Column("Комментарий", "text"),
        ),
        rollup="sum_by_period(Дата, Сумма)",
    ),
    Category(
        name="loan",
        worksheet="Loans",
        when_to_use="деньги в долг или возврат долга",
        columns=(
            Column("Сумма", "money"),
            Column("Валюта", "currency"),
            Column("Заёмщик", "text"),
            Column("Дата", "date"),
            Column("Комментарий", "text"),
        ),
        rollup="balance(Заёмщик, Сумма)",
    ),
    Category(
        name="telemetry",
        worksheet="Telemetry",
        when_to_use="измерение о себе",
        columns=(
            Column("Метрика", "text"),
            Column("Значение", "number"),
            Column("Ед", "text"),
            Column("Дата", "date"),
        ),
    ),
    Category(
        name="wish",
        worksheet="Wishlist",
        when_to_use="чего хочется",
        columns=(
            Column("Желание", "text"),
            Column("Добавлено", "date"),
            Column("Исполнено", "text"),
        ),
    ),
    FACTS_FALLBACK,
)


def parse_columns(cell: str) -> tuple[tuple[Column, ...], tuple[str, ...]]:
    """Parse a `columns` cell: `"Сумма:money, Комментарий"`.

    Returns the columns and any validation errors. On any error the caller
    drops the whole row — a category with a column it cannot type is worse
    than no category.
    """
    columns: list[Column] = []
    errors: list[str] = []
    seen: set[str] = set()

    for raw in cell.split(","):
        spec = raw.strip()
        if not spec:
            continue
        header, _, type_name = spec.partition(":")
        header = header.strip()
        type_name = type_name.strip().lower() or DEFAULT_FIELD_TYPE
        if not header:
            errors.append(f"column spec {spec!r} has no header")
            continue
        if header == KEY_HEADER:
            errors.append(f"{KEY_HEADER!r} is the reserved key column header")
            continue
        if header in seen:
            errors.append(f"duplicate column header {header!r}")
            continue
        if type_name not in FIELD_TYPES:
            errors.append(
                f"column {header!r} has unknown type {type_name!r}; "
                f"known types are {sorted(FIELD_TYPES)}"
            )
            continue
        seen.add(header)
        columns.append(Column(header, type_name))

    if not columns and not errors:
        errors.append("no columns declared")
    return tuple(columns), tuple(errors)


def _parse_rollup(
    cell: str, columns: tuple[Column, ...]
) -> tuple[str | None, list[str]]:
    """Validate a `rollup` cell. A bad rollup drops the rollup, not the row."""
    cell = cell.strip()
    if not cell:
        return None, []

    match = _ROLLUP_RE.match(cell)
    if not match:
        return None, [f"rollup {cell!r} is not of the form name(col, col)"]

    func, args_src = match.group(1), match.group(2)
    if func not in ROLLUP_TEMPLATES:
        return None, [
            f"unknown rollup {func!r}; known rollups are {sorted(ROLLUP_TEMPLATES)}"
        ]

    headers = {c.header for c in columns}
    args = [a.strip() for a in args_src.split(",") if a.strip()]
    if not args:
        return None, [f"rollup {cell!r} names no columns"]
    missing = [a for a in args if a not in headers]
    if missing:
        return None, [f"rollup {cell!r} names columns that do not exist: {missing}"]
    return cell, []


def _cell(row: list[str], index: int) -> str:
    """Read a cell from a possibly-short row. Sheets truncates trailing blanks."""
    return row[index].strip() if index < len(row) and row[index] else ""


def parse_registry(rows: list[list[str]]) -> Registry:
    """Parse the `_categories` worksheet values, header row included."""
    categories: list[Category] = []
    errors: list[str] = []
    names: set[str] = set()
    worksheets: set[str] = set()

    for number, row in enumerate(rows[1:], start=2):
        name = _cell(row, 0)
        worksheet = _cell(row, 1)
        when_to_use = _cell(row, 2)
        columns_cell = _cell(row, 3)
        rollup_cell = _cell(row, 4)

        if not any((name, worksheet, when_to_use, columns_cell, rollup_cell)):
            continue  # a blank row is not an error

        where = f"row {number}"
        if not name:
            errors.append(f"{where}: no category name")
            continue
        if not worksheet:
            errors.append(f"{where} ({name}): no worksheet")
            continue
        if name in names:
            errors.append(f"{where}: duplicate category name {name!r}")
            continue
        if worksheet in worksheets:
            errors.append(f"{where} ({name}): worksheet {worksheet!r} already used")
            continue

        columns, column_errors = parse_columns(columns_cell)
        if column_errors:
            errors.extend(f"{where} ({name}): {e}" for e in column_errors)
            continue

        rollup, rollup_errors = _parse_rollup(rollup_cell, columns)
        errors.extend(f"{where} ({name}): {e}" for e in rollup_errors)

        names.add(name)
        worksheets.add(worksheet)
        categories.append(
            Category(
                name=name,
                worksheet=worksheet,
                when_to_use=when_to_use,
                columns=columns,
                rollup=rollup,
            )
        )

    if FACTS_FALLBACK.name not in names:
        categories.append(FACTS_FALLBACK)

    return Registry(tuple(categories), tuple(errors))


def to_rows(categories: tuple[Category, ...]) -> list[list[str]]:
    """Render categories back to `_categories` rows, for seeding."""
    return [
        [
            c.name,
            c.worksheet,
            c.when_to_use,
            ", ".join(
                col.header
                if col.type == DEFAULT_FIELD_TYPE
                else f"{col.header}:{col.type}"
                for col in c.columns
            ),
            c.rollup or "",
        ]
        for c in categories
    ]


#: The registry when there is no workbook at all. The workbook is a
#: projection, so ingestion must not depend on one existing: with no
#: workbook the seeded categories *are* the registry, and being able to
#: edit them is what linking a workbook buys.
NO_SHEET_REGISTRY = Registry(SEED_CATEGORIES)

_cache: dict[str, tuple[float, Registry]] = {}


def _worksheet(ags: AsyncioGspreadSpreadsheet, headers: list[str]) -> Worksheet:
    """Factory seam, so load_registry is testable without Sheets."""
    from telegrind.sheets import Worksheet

    return Worksheet(ags, REGISTRY_WORKSHEET, headers)


def invalidate(cache_key: str | None = None) -> None:
    """Drop the cached registry for one chat, or for all of them."""
    if cache_key is None:
        _cache.clear()
    else:
        _cache.pop(cache_key, None)


async def load_registry(
    ags: AsyncioGspreadSpreadsheet | None,
    cache_key: str,
    *,
    now: float | None = None,
) -> Registry:
    """Read `_categories`, seeding it if empty. Cached for CACHE_TTL_SECONDS.

    With no workbook there is nothing to read and nothing worth caching —
    the seeded categories are a constant.

    `cache_key` is the chat, not the workbook URL, because a chat with no
    workbook still has a registry and still needs /reload to reach it. The
    URL made `invalidate(chat.sheet_url)` mean "every chat" the moment the
    URL was None.

    `now` is injectable so the TTL is testable without sleeping.
    """
    if ags is None:
        return NO_SHEET_REGISTRY

    at = time.monotonic() if now is None else now
    cached = _cache.get(cache_key)
    if cached and at - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    ws = _worksheet(ags, REGISTRY_HEADERS)
    rows = await ws.all_values()

    if len(rows) <= 1:
        # Absent or header-only: seed the five defaults. The worksheets
        # themselves are still created lazily on first write, so a seeded
        # category you never use adds no clutter.
        #
        # `Worksheet.agw()` guarantees row 1 is the header — on the created
        # path and on a found-but-empty one — so this appends below it.
        await ws.append(to_rows(SEED_CATEGORIES))
        rows = await ws.all_values()

    registry = parse_registry(rows)
    for error in registry.errors:
        log.warning("_categories: %s", error)

    _cache[cache_key] = (at, registry)
    return registry
