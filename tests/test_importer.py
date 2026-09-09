from test_sheets import FakeAgs, FakeAgw

from telegrind.importer import (
    ImportedRow,
    map_row,
    plan_import,
    plan_worksheet,
    synthesized_key,
)
from telegrind.registry import KEY_HEADER, Category, Column, Registry

EXPENSE = Category(
    name="expense",
    worksheet="Expenses",
    when_to_use="?",
    columns=(
        Column("Сумма", "money"),
        Column("Валюта", "currency"),
        Column("Дата", "date"),
        Column("Комментарий", "text"),
    ),
)
SHEET_HEADERS = ["#", "Сумма", "Валюта", "Дата", "Комментарий"]


def test_synthesized_key_is_stable_and_identifiable() -> None:
    assert synthesized_key("Expenses", 7) == "import-Expenses-7"


def test_map_row_keeps_the_telegram_key_from_column_a() -> None:
    row = map_row(
        EXPENSE, SHEET_HEADERS, ["4821", "4500", "KZT", "09.09.26 21:40", "такси"], 2
    )
    assert row.sheet_key == "4821"
    assert row.synthesized is False
    assert row.fields == {
        "Сумма": "4500",
        "Валюта": "KZT",
        "Дата": "09.09.26 21:40",
        "Комментарий": "такси",
    }


def test_map_row_synthesizes_a_key_for_a_hand_added_row() -> None:
    row = map_row(EXPENSE, SHEET_HEADERS, ["", "4500", "KZT", "", "наличкой"], 9)
    assert row.sheet_key == "import-Expenses-9"
    assert row.synthesized is True


def test_map_row_reads_columns_by_header_not_by_position() -> None:
    """A user-added column must not shift the fields it sits before."""
    headers = ["#", "Сумма", "Мой столбец", "Валюта", "Дата", "Комментарий"]
    row = map_row(
        EXPENSE,
        headers,
        ["4821", "4500", "заметка", "USD", "09.09.26 21:40", "такси"],
        2,
    )
    assert row.fields["Валюта"] == "USD"
    assert row.fields["Комментарий"] == "такси"
    assert "Мой столбец" not in row.fields


def test_map_row_fills_a_column_the_sheet_does_not_have() -> None:
    row = map_row(EXPENSE, ["#", "Сумма"], ["4821", "4500"], 2)
    assert row.fields["Комментарий"] == ""
    assert row.fields["Валюта"] == ""


def test_map_row_tolerates_a_short_row() -> None:
    row = map_row(EXPENSE, SHEET_HEADERS, ["4821", "4500"], 2)
    assert row.fields["Дата"] == ""


def test_plan_worksheet_skips_the_header_row() -> None:
    rows = plan_worksheet(
        EXPENSE,
        [SHEET_HEADERS, ["4821", "4500", "KZT", "09.09.26 21:40", "такси"]],
    )
    assert len(rows) == 1
    assert rows[0].sheet_key == "4821"


def test_plan_worksheet_of_an_empty_sheet_is_empty() -> None:
    assert plan_worksheet(EXPENSE, []) == []
    assert plan_worksheet(EXPENSE, [SHEET_HEADERS]) == []


def test_plan_worksheet_skips_a_fully_blank_row() -> None:
    rows = plan_worksheet(
        EXPENSE,
        [SHEET_HEADERS, ["", "", "", "", ""], ["4821", "1", "KZT", "", ""]],
    )
    assert [r.sheet_key for r in rows] == ["4821"]


def test_plan_worksheet_numbers_synthesized_keys_by_real_row_number() -> None:
    rows = plan_worksheet(
        EXPENSE,
        [SHEET_HEADERS, ["", "1", "KZT", "", ""], ["", "2", "KZT", "", ""]],
    )
    assert [r.sheet_key for r in rows] == ["import-Expenses-2", "import-Expenses-3"]


def test_plan_worksheet_deduplicates_a_repeated_key() -> None:
    """Column A is a Telegram message_id; a duplicate would collide on the
    (chat_pk, worksheet, sheet_key) unique constraint."""
    rows = plan_worksheet(
        EXPENSE,
        [SHEET_HEADERS, ["4821", "1", "KZT", "", ""], ["4821", "2", "KZT", "", ""]],
    )
    keys = [r.sheet_key for r in rows]
    assert len(set(keys)) == 2
    assert keys[0] == "4821"


def test_imported_row_is_a_value() -> None:
    assert ImportedRow("4821", {}, False) == ImportedRow("4821", {}, False)


async def test_plan_import_skips_a_worksheet_whose_headers_collide() -> None:
    """One collided sheet must not abort the other categories' import."""
    expenses = Category(
        name="expense",
        worksheet="Expenses",
        when_to_use="",
        columns=(Column("Сумма", "money"),),
    )
    wishes = Category(
        name="wish",
        worksheet="Wishlist",
        when_to_use="",
        columns=(Column("Желание", "text"),),
    )
    ags = FakeAgs(
        {
            # Row 1 declares the user's own column where `Сумма` should be.
            "Expenses": FakeAgw([[KEY_HEADER, "Курс"], ["1_1", "1.0"]]),
            "Wishlist": FakeAgw([[KEY_HEADER, "Желание"], ["2_1", "монитор"]]),
        }
    )
    plan, refused = await plan_import(ags, Registry((expenses, wishes)))
    assert list(plan) == ["wish"]
    assert "Expenses" in refused
