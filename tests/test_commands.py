from telegrind.bot.handlers.commands import (
    format_import_plan,
    format_rebuild_report,
)
from telegrind.importer import ImportedRow
from telegrind.projection import RebuildReport


def test_import_plan_lists_each_category_with_a_count() -> None:
    out = format_import_plan(
        {
            "expense": [ImportedRow("1", {}, False), ImportedRow("2", {}, False)],
            "wish": [ImportedRow("3", {}, False)],
        }
    )
    assert "expense" in out
    assert "2" in out
    assert "wish" in out


def test_import_plan_counts_synthesized_keys_separately() -> None:
    out = format_import_plan(
        {
            "expense": [
                ImportedRow("1", {}, False),
                ImportedRow("import-Expenses-3", {}, True),
            ]
        }
    )
    assert "2" in out
    assert "1" in out


def test_an_empty_import_plan_says_so() -> None:
    assert "нечего" in format_import_plan({}).lower()


def test_rebuild_report_lists_what_was_rewritten() -> None:
    out = format_rebuild_report(RebuildReport(rebuilt={"Expenses": 412}, refused={}))
    assert "Expenses" in out
    assert "412" in out


def test_rebuild_report_names_refusals_and_how_to_recover() -> None:
    out = format_rebuild_report(RebuildReport(rebuilt={}, refused={"Loans": 37}))
    assert "Loans" in out
    assert "37" in out
    assert "--force" in out
    assert "/import" in out


def test_a_rebuild_that_did_nothing_still_says_something() -> None:
    assert format_rebuild_report(RebuildReport()).strip()


def test_rebuild_report_names_a_worksheet_the_api_rejected() -> None:
    """One worksheet's API error must be reported, not swallowed with the
    rest of the run — /rebuild died mid-walk on Telemetry in live testing
    and the user got no reply at all."""
    out = format_rebuild_report(
        RebuildReport(rebuilt={"Expenses": 3501}, failed={"Telemetry": "exceeds grid"})
    )
    assert "Expenses" in out
    assert "3501" in out
    assert "Telemetry" in out
    assert "exceeds grid" in out


def test_a_report_with_a_failure_is_not_ok() -> None:
    assert not RebuildReport(failed={"Telemetry": "boom"}).ok
    assert not RebuildReport(refused={"Loans": 3}).ok
    assert RebuildReport(rebuilt={"Expenses": 1}).ok


def test_format_import_plan_reports_a_refused_worksheet() -> None:
    """A collided sheet is skipped, not silently absent from the report."""
    out = format_import_plan({}, {"Expenses": "Expenses!C1 holds 'Курс'"})
    assert "Expenses" in out
    assert "Курс" in out
