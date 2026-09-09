from telegrind.registry import (
    FACTS_FALLBACK,
    REGISTRY_HEADERS,
    SEED_CATEGORIES,
    Column,
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
