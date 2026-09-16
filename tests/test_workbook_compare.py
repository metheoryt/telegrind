from datetime import UTC, datetime
from pathlib import Path

from telegrind.models import Fact, LoggedMessage
from telegrind.workbook_compare import message_id_of, read_sheet, report

AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_a_bare_id_is_the_pre_9540232_form() -> None:
    assert message_id_of("641715") == 641715


def test_an_id_with_a_seq_is_the_later_form() -> None:
    assert message_id_of("641715_2") == 641715


def test_a_key_that_is_not_an_id_is_none() -> None:
    assert message_id_of("") is None
    assert message_id_of("итого") is None


def test_a_repeated_key_drops_both_and_is_counted(tmp_path: Path) -> None:
    """A duplicate is a connectivity retry, and a labelled set whose
    labels are guesses is worth nothing — drop and count, never repair."""
    path = tmp_path / "Outcome.csv"
    path.write_text(
        "key,amount,category\n641715,449,tech\n641715,500,tech\n", encoding="utf-8"
    )
    rows, dropped = read_sheet(path)
    assert rows == {}
    assert dropped["duplicate"] == 2


def test_a_key_repeated_three_times_drops_all_three(tmp_path: Path) -> None:
    """The counting pass happens before any row is kept, so a key seen
    three times must drop all three — never «the first one wins»."""
    path = tmp_path / "Outcome.csv"
    path.write_text(
        "key,amount\n641715,449\n641715,450\n641715,451\n",
        encoding="utf-8",
    )
    rows, dropped = read_sheet(path)
    assert rows == {}
    assert dropped["duplicate"] == 3


def test_distinct_keys_survive(tmp_path: Path) -> None:
    path = tmp_path / "Outcome.csv"
    path.write_text("key,amount\n641715_1,449\n641715_2,500\n", encoding="utf-8")
    rows, dropped = read_sheet(path)
    assert sorted(rows) == ["641715_1", "641715_2"]
    assert rows["641715_1"]["amount"] == "449"
    assert not dropped


class FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> list[object]:
        return self._rows


class FakeSession:
    """Enough of an AsyncSession for `report`: two selects, in order —
    messages first, then facts — with no write in between."""

    def __init__(self, *result_rows: list[object]) -> None:
        self._queue = [FakeResult(rows) for rows in result_rows]

    async def execute(self, statement: object) -> FakeResult:
        return self._queue.pop(0)


def logged(pk: int, message_id: int, text: str | None) -> LoggedMessage:
    return LoggedMessage(
        id=pk,
        chat_pk=1,
        message_id=message_id,
        kind="text",
        text=text,
        tg_date=AT,
        raw={},
    )


def fact(message_pk: int, kind: str = "expense") -> Fact:
    return Fact(
        chat_pk=1,
        message_pk=message_pk,
        seq=1,
        kind=kind,
        at=AT,
        fields={"amount": 4500},
    )


async def test_report_matches_missing_and_no_fact(tmp_path: Path) -> None:
    matched_row = logged(1, 641715, "4500 такси")
    bare_row = logged(2, 641717, "просто болтовня")
    session = FakeSession([matched_row, bare_row], [fact(message_pk=1)])
    sheets = {
        "Outcome": {
            "641715": {"key": "641715", "amount": "449"},
            "641716": {"key": "641716", "amount": "0"},
            "641717": {"key": "641717", "amount": "1"},
        }
    }
    out = tmp_path / "report.tsv"

    tally = await report(session, chat_pk=1, sheets=sheets, out=out)

    assert tally["matched"] == 1
    assert tally["missing_message"] == 1
    assert tally["no_fact"] == 1
    text = out.read_text(encoding="utf-8")
    assert "641715" in text
    assert "MISSING" in text
    assert "641716" in text
