from datetime import datetime
from types import SimpleNamespace

import pytest

from telegrind.config import ChatConfig
from telegrind.query import Answer, Row, Spec, Unanswerable, run

CFG = ChatConfig(tz_offset=6, currency="KZT")


class FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.statements: list[object] = []

    async def execute(self, statement: object) -> SimpleNamespace:
        self.statements.append(statement)
        rows = self.rows
        return SimpleNamespace(
            all=lambda: rows, first=lambda: rows[0] if rows else None
        )


def test_parse_builds_a_half_open_period() -> None:
    spec = Spec.parse(
        {
            "kinds": ["expense"],
            "aggregate": "sum",
            "field": "amount",
            "since": "2026-08-01",
            "until": "2026-09-01",
        },
        CFG,
    )

    assert spec.kinds == ("expense",)
    assert spec.since == datetime(2026, 8, 1, tzinfo=CFG.tz)
    assert spec.until == datetime(2026, 9, 1, tzinfo=CFG.tz)


def test_an_unknown_aggregate_is_unanswerable() -> None:
    with pytest.raises(Unanswerable):
        Spec.parse({"kinds": ["expense"], "aggregate": "median"}, CFG)


def test_a_numeric_aggregate_without_a_field_is_unanswerable() -> None:
    with pytest.raises(Unanswerable):
        Spec.parse({"kinds": ["expense"], "aggregate": "sum"}, CFG)


def test_count_needs_no_field() -> None:
    assert Spec.parse({"kinds": ["habit"], "aggregate": "count"}, CFG).field is None


def test_balance_by_needs_something_to_group_on() -> None:
    with pytest.raises(Unanswerable):
        Spec.parse(
            {"kinds": ["loan"], "aggregate": "balance_by", "field": "amount"}, CFG
        )


async def test_run_guards_the_cast_and_reports_what_it_skipped() -> None:
    session = FakeSession([(None, 12300.0, 4, 1)])
    spec = Spec.parse(
        {"kinds": ["expense"], "aggregate": "sum", "field": "amount"}, CFG
    )

    answer = await run(session, chat_pk=1, spec=spec)

    assert answer == Answer(rows=[Row(group=None, value=12300.0, n=4)], skipped=1)
    rendered = str(session.statements[-1])
    assert "jsonb_typeof" in rendered
    assert "deleted_at IS NULL" in rendered


async def test_run_groups_when_asked() -> None:
    session = FakeSession([("Вася", -500.0, 2, 0), ("Петя", 1000.0, 1, 0)])
    spec = Spec.parse(
        {
            "kinds": ["loan"],
            "aggregate": "balance_by",
            "field": "amount",
            "group_by": "counterparty",
        },
        CFG,
    )

    answer = await run(session, chat_pk=1, spec=spec)

    assert [row.group for row in answer.rows] == ["Вася", "Петя"]
