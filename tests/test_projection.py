import re

from telegrind.llm import RawFact
from telegrind.models import Fact
from telegrind.projection import (
    ChangeKind,
    diff_facts,
    fact_row,
    sheet_key,
)
from telegrind.registry import Category, Column

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


def stored(seq: int, category: str) -> Fact:
    return Fact(
        seq=seq,
        category=category,
        fields={},
        worksheet=category.title(),
        sheet_key=sheet_key(4821, seq),
    )


def test_sheet_key_joins_message_id_and_seq() -> None:
    assert sheet_key(4821, 1) == "4821_1"
    assert sheet_key(4821, 2) == "4821_2"


def test_sheet_key_is_uniform_for_single_fact_messages() -> None:
    """A message can gain a second fact on a later edit, so 1 is not special."""
    assert sheet_key(7, 1) == "7_1"


def test_sheet_key_is_never_a_number_sheets_could_coerce() -> None:
    """Column A is written USER_ENTERED. A key Sheets parses as a number is
    re-rendered on read: "4821.10" would come back "4821.1" and collide with
    seq 1, so find_key would never match the tenth fact of a message.

    The oracle is a regex, not `float()`: Python accepts `_` as a digit
    separator, so `float("4821_1")` is 48211.0, while Sheets has no such
    syntax and leaves the same string as text. Do not "fix" this back.
    """
    numeric = re.compile(r"[0-9]+(?:[.,][0-9]+)?")
    for seq in (1, 2, 10, 100):
        assert not numeric.fullmatch(sheet_key(4821, seq))


def test_fact_row_puts_the_key_in_column_a_then_the_columns_in_order() -> None:
    row = fact_row(
        EXPENSE,
        "4821_1",
        {
            "Сумма": 4500.0,
            "Валюта": "KZT",
            "Дата": "09.09.26 21:40",
            "Комментарий": "такси",
        },
    )
    assert row == ["4821_1", 4500.0, "KZT", "09.09.26 21:40", "такси"]


def test_fact_row_fills_a_missing_field_with_an_empty_string() -> None:
    row = fact_row(EXPENSE, "4821_1", {"Сумма": 4500.0})
    assert row == ["4821_1", 4500.0, "", "", ""]


def test_fact_row_is_always_the_full_declared_width() -> None:
    """Worksheet.update_row writes left-to-right from A and does not clear
    what it does not reach, so a short row would leave stale trailing cells
    behind on a REWRITE."""
    assert len(fact_row(EXPENSE, "4821_1", {})) == len(EXPENSE.columns) + 1


def test_fact_row_ignores_a_field_the_category_does_not_declare() -> None:
    row = fact_row(EXPENSE, "4821_1", {"Сумма": 1.0, "Придумано": "x"})
    assert len(row) == 5


def test_diff_same_seq_same_category_rewrites_in_place() -> None:
    changes = diff_facts([stored(1, "expense")], [RawFact("expense", {"Сумма": 5000})])
    assert [c.kind for c in changes] == [ChangeKind.REWRITE]
    assert changes[0].seq == 1
    assert changes[0].fact is not None
    assert changes[0].raw is not None


def test_diff_same_seq_different_category_moves() -> None:
    changes = diff_facts(
        [stored(1, "expense")], [RawFact("telemetry", {"Значение": 82.4})]
    )
    assert [c.kind for c in changes] == [ChangeKind.MOVE]
    assert changes[0].fact is not None
    assert changes[0].fact.category == "expense"
    assert changes[0].raw is not None
    assert changes[0].raw.category == "telemetry"


def test_diff_seq_gone_deletes() -> None:
    changes = diff_facts(
        [stored(1, "expense"), stored(2, "telemetry")], [RawFact("expense", {})]
    )
    assert [c.kind for c in changes] == [ChangeKind.REWRITE, ChangeKind.DELETE]
    assert changes[1].seq == 2
    assert changes[1].raw is None


def test_diff_new_seq_appends() -> None:
    changes = diff_facts(
        [stored(1, "expense")],
        [RawFact("expense", {}), RawFact("telemetry", {})],
    )
    assert [c.kind for c in changes] == [ChangeKind.REWRITE, ChangeKind.APPEND]
    assert changes[1].seq == 2
    assert changes[1].fact is None


def test_diff_of_a_first_extraction_is_all_appends() -> None:
    changes = diff_facts([], [RawFact("expense", {}), RawFact("telemetry", {})])
    assert [c.kind for c in changes] == [ChangeKind.APPEND, ChangeKind.APPEND]
    assert [c.seq for c in changes] == [1, 2]


def test_diff_of_an_edit_that_extracts_nothing_deletes_everything() -> None:
    changes = diff_facts([stored(1, "expense"), stored(2, "expense")], [])
    assert [c.kind for c in changes] == [ChangeKind.DELETE, ChangeKind.DELETE]


def test_diff_of_no_change_still_rewrites() -> None:
    """Cheap and idempotent beats a field-by-field comparison that can be wrong."""
    changes = diff_facts([stored(1, "expense")], [RawFact("expense", {})])
    assert [c.kind for c in changes] == [ChangeKind.REWRITE]


def test_diff_is_ordered_by_seq() -> None:
    changes = diff_facts(
        [stored(2, "expense"), stored(1, "expense")],
        [RawFact("expense", {}), RawFact("expense", {}), RawFact("expense", {})],
    )
    assert [c.seq for c in changes] == [1, 2, 3]
