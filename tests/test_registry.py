import pytest

import telegrind.registry as registry_module
from telegrind.registry import (
    CACHE_TTL_SECONDS,
    FACTS_FALLBACK,
    REGISTRY_HEADERS,
    SEED_CATEGORIES,
    Column,
    invalidate,
    load_registry,
    parse_columns,
    parse_registry,
    to_rows,
)


def rows(*body: list[str]) -> list[list[str]]:
    return [REGISTRY_HEADERS, *body]


def test_parses_a_single_category() -> None:
    reg = parse_registry(
        rows(["expense", "Expenses", "потрачено", "Сумма:money, Комментарий", ""])
    )
    assert reg.errors == ()
    cat = reg.by_name("expense")
    assert cat is not None
    assert cat.worksheet == "Expenses"
    assert cat.when_to_use == "потрачено"
    assert cat.columns == (Column("Сумма", "money"), Column("Комментарий", "text"))
    assert cat.rollup is None


def test_type_defaults_to_text_when_omitted() -> None:
    cols, errors = parse_columns("Желание, Добавлено:date")
    assert errors == ()
    assert cols == (Column("Желание", "text"), Column("Добавлено", "date"))


def test_headers_prefix_the_key_column() -> None:
    cat = parse_registry(
        rows(["wish", "Wishlist", "чего хочется", "Желание, Добавлено:date", ""])
    ).by_name("wish")
    assert cat is not None
    assert cat.headers == ["#", "Желание", "Добавлено"]


def test_unknown_type_drops_the_row_with_an_error() -> None:
    reg = parse_registry(rows(["weird", "Weird", "?", "Значение:quantum", ""]))
    assert reg.by_name("weird") is None
    assert any("quantum" in e for e in reg.errors)


def test_missing_name_drops_the_row() -> None:
    reg = parse_registry(rows(["", "Nameless", "?", "Текст", ""]))
    assert len(reg.categories) == 1  # only the injected facts fallback
    assert any("name" in e.lower() for e in reg.errors)


def test_missing_worksheet_drops_the_row() -> None:
    reg = parse_registry(rows(["orphan", "", "?", "Текст", ""]))
    assert reg.by_name("orphan") is None
    assert any("worksheet" in e.lower() for e in reg.errors)


def test_no_columns_drops_the_row() -> None:
    reg = parse_registry(rows(["empty", "Empty", "?", "   ", ""]))
    assert reg.by_name("empty") is None
    assert any("column" in e.lower() for e in reg.errors)


def test_duplicate_name_keeps_the_first_and_reports_the_second() -> None:
    reg = parse_registry(
        rows(
            ["expense", "Expenses", "a", "Сумма:money", ""],
            ["expense", "Other", "b", "Значение:number", ""],
        )
    )
    cat = reg.by_name("expense")
    assert cat is not None
    assert cat.worksheet == "Expenses"
    assert any("duplicate" in e.lower() for e in reg.errors)


def test_duplicate_worksheet_drops_the_second_row() -> None:
    reg = parse_registry(
        rows(
            ["a", "Shared", "x", "Текст", ""],
            ["b", "Shared", "y", "Текст", ""],
        )
    )
    assert reg.by_name("a") is not None
    assert reg.by_name("b") is None
    assert any("worksheet" in e.lower() for e in reg.errors)


def test_duplicate_column_header_drops_the_row() -> None:
    reg = parse_registry(rows(["dup", "Dup", "?", "Сумма:money, Сумма:number", ""]))
    assert reg.by_name("dup") is None
    assert any("Сумма" in e for e in reg.errors)


def test_key_column_header_is_reserved() -> None:
    reg = parse_registry(rows(["bad", "Bad", "?", "#:text, Сумма:money", ""]))
    assert reg.by_name("bad") is None
    assert any("#" in e for e in reg.errors)


def test_rollup_is_kept_when_it_names_real_columns() -> None:
    cat = parse_registry(
        rows(
            [
                "loan",
                "Loans",
                "долг",
                "Сумма:money, Заёмщик:text",
                "balance(Заёмщик, Сумма)",
            ]
        )
    ).by_name("loan")
    assert cat is not None
    assert cat.rollup == "balance(Заёмщик, Сумма)"


def test_unknown_rollup_function_is_an_error_and_the_rollup_is_dropped() -> None:
    reg = parse_registry(
        rows(["loan", "Loans", "долг", "Сумма:money", "teleport(Сумма)"])
    )
    cat = reg.by_name("loan")
    assert cat is not None
    assert cat.rollup is None
    assert any("teleport" in e for e in reg.errors)


def test_rollup_naming_a_missing_column_is_an_error() -> None:
    reg = parse_registry(
        rows(["loan", "Loans", "долг", "Сумма:money", "balance(Кто, Сумма)"])
    )
    cat = reg.by_name("loan")
    assert cat is not None
    assert cat.rollup is None
    assert any("Кто" in e for e in reg.errors)


def test_malformed_rollup_syntax_is_an_error_not_a_crash() -> None:
    reg = parse_registry(rows(["loan", "Loans", "долг", "Сумма:money", "balance("]))
    assert reg.by_name("loan") is not None
    assert reg.errors != ()


def test_short_rows_do_not_raise() -> None:
    reg = parse_registry([REGISTRY_HEADERS, ["expense"], [], ["a", "B"]])
    assert reg.errors != ()
    assert reg.by_name("facts") is not None


def test_facts_fallback_is_injected_when_absent() -> None:
    reg = parse_registry(rows(["expense", "Expenses", "?", "Сумма:money", ""]))
    assert reg.by_name("facts") == FACTS_FALLBACK


def test_a_user_defined_facts_row_wins_over_the_fallback() -> None:
    reg = parse_registry(rows(["facts", "Журнал", "всё", "Текст, Дата:date", ""]))
    cat = reg.by_name("facts")
    assert cat is not None
    assert cat.worksheet == "Журнал"


def test_blank_rows_are_ignored_silently() -> None:
    reg = parse_registry(rows(["", "", "", "", ""], ["  ", "", "", "", ""]))
    assert reg.errors == ()
    assert reg.categories == (FACTS_FALLBACK,)


def test_seed_reproduces_todays_worksheet_headers_exactly() -> None:
    by_name = {c.name: c for c in SEED_CATEGORIES}
    assert by_name["expense"].headers == ["#", "Сумма", "Валюта", "Дата", "Комментарий"]
    assert by_name["loan"].headers == [
        "#",
        "Сумма",
        "Валюта",
        "Заёмщик",
        "Дата",
        "Комментарий",
    ]
    assert by_name["wish"].headers == ["#", "Желание", "Добавлено", "Исполнено"]


def test_to_rows_round_trips_through_parse_registry() -> None:
    reg = parse_registry([REGISTRY_HEADERS, *to_rows(SEED_CATEGORIES)])
    assert reg.errors == ()
    assert reg.categories == SEED_CATEGORIES


class FakeWorksheet:
    """Stands in for telegrind.sheets.Worksheet. Counts round trips.

    Mirrors the real client's invariant that row 1 is always the header —
    `Worksheet.agw()` writes it on create and repairs a found-but-empty
    sheet, so `all_values()` never returns a sheet with no header at all.
    See tests/test_sheets.py for the guard itself.
    """

    def __init__(self, values: list[list[str]]) -> None:
        self.values = values or [REGISTRY_HEADERS]
        self.reads = 0
        self.appended: list[list[str]] = []

    async def all_values(self) -> list[list[str]]:
        self.reads += 1
        return self.values

    async def append(self, rows: list[list[object]]) -> None:
        self.appended.extend(rows)
        self.values = self.values + [[str(c) for c in r] for r in rows]


def _one_category() -> FakeWorksheet:
    return FakeWorksheet(
        [REGISTRY_HEADERS, ["expense", "Expenses", "?", "Сумма:money", ""]]
    )


async def test_load_registry_parses_the_worksheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = _one_category()
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    reg = await load_registry(object(), "url-a", now=0.0)
    assert reg.by_name("expense") is not None
    assert ws.reads == 1


async def test_load_registry_caches_within_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = _one_category()
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-b", now=100.0)
    await load_registry(object(), "url-b", now=100.0 + CACHE_TTL_SECONDS - 1)
    assert ws.reads == 1


async def test_load_registry_refreshes_after_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = _one_category()
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-c", now=100.0)
    await load_registry(object(), "url-c", now=100.0 + CACHE_TTL_SECONDS + 1)
    assert ws.reads == 2


async def test_the_cache_is_keyed_by_sheet_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = _one_category()
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-d", now=0.0)
    await load_registry(object(), "url-e", now=0.0)
    assert ws.reads == 2


async def test_invalidate_drops_one_url(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = _one_category()
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-f", now=0.0)
    invalidate("url-f")
    await load_registry(object(), "url-f", now=0.0)
    assert ws.reads == 2


async def test_an_empty_registry_worksheet_is_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = FakeWorksheet([])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    reg = await load_registry(object(), "url-g", now=0.0)
    assert ws.appended == to_rows(SEED_CATEGORIES)
    assert reg.categories == SEED_CATEGORIES


async def test_a_header_only_registry_worksheet_is_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-h", now=0.0)
    assert ws.appended == to_rows(SEED_CATEGORIES)


async def test_a_populated_registry_worksheet_is_never_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["only", "Only", "?", "Текст", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    reg = await load_registry(object(), "url-i", now=0.0)
    assert ws.appended == []
    assert reg.by_name("only") is not None


async def test_load_registry_with_no_workbook_returns_the_seeded_categories() -> None:
    """Extraction has to work before any spreadsheet exists.

    `_categories` is a sheet the user may edit, but it cannot be a
    *prerequisite* for recording a fact — that was the onboarding gate, and
    it is what made the bot's first answer a refusal. With no workbook the
    seeds are the registry.
    """
    invalidate()
    reg = await load_registry(None, "chat-1")
    assert reg.categories == SEED_CATEGORIES
    assert reg.errors == ()
    assert reg.by_name("expense") is not None
    assert reg.by_name("facts") is not None
