"""A question becomes a constrained spec, and the spec becomes SQL.

The model never does arithmetic. It says what to count and over what;
Postgres does the counting. A question this spec cannot express is
answered honestly rather than approximately — text-to-SQL as an escape
hatch is a non-goal.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, func, null, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.config import ChatConfig
from telegrind.models import Fact

#: Closed on purpose. Expenses sum; weight is a series; habits are counts;
#: debts are a running sum per counterparty.
AGGREGATES = ("sum", "count", "avg", "min", "max", "last", "balance_by")

#: Everything but `count` reads a number out of JSONB.
NEEDS_FIELD = ("sum", "avg", "min", "max", "last", "balance_by")


class Unanswerable(Exception):  # noqa: N818 — reads as the refusal, not an error
    """The question does not fit the spec. Say so; do not guess a number."""


@dataclass(frozen=True, slots=True)
class Spec:
    kinds: tuple[str, ...]
    aggregate: str
    field: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    group_by: str | None = None
    filters: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, payload: dict, cfg: ChatConfig) -> Spec:
        aggregate = str(payload.get("aggregate") or "")
        if aggregate not in AGGREGATES:
            raise Unanswerable(f"unknown aggregate {aggregate!r}")

        field = payload.get("field") or None
        if aggregate in NEEDS_FIELD and not field:
            raise Unanswerable(f"{aggregate} needs a field")

        group_by = payload.get("group_by") or None
        if aggregate == "balance_by" and not group_by:
            raise Unanswerable("balance_by needs something to group on")

        return cls(
            kinds=tuple(str(k) for k in (payload.get("kinds") or [])),
            aggregate=aggregate,
            field=str(field) if field else None,
            since=_boundary(payload.get("since"), cfg),
            until=_boundary(payload.get("until"), cfg),
            group_by=str(group_by) if group_by else None,
            filters=tuple(
                (str(f["field"]), str(f["value"]))
                for f in (payload.get("filters") or [])
                if f.get("field")
            ),
        )


def _boundary(raw: object, cfg: ChatConfig) -> datetime | None:
    """An ISO date from the model, in the chat's own timezone.

    Not `dateparser`: a period is a month boundary as often as not, and a
    relative parser turns «август» into *this day* in August.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise Unanswerable(f"bad period boundary {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=cfg.tz)


@dataclass(frozen=True, slots=True)
class Row:
    group: str | None
    value: float | None
    n: int


@dataclass(frozen=True, slots=True)
class Answer:
    rows: list[Row]
    #: Facts that matched but whose number was not a number. Reported, so
    #: a total is never quietly short.
    skipped: int


async def run(session: AsyncSession, chat_pk: int, spec: Spec) -> Answer:
    """Run the spec. Every number in an answer comes out of here."""
    where: list[Any] = [Fact.chat_pk == chat_pk, Fact.deleted_at.is_(None)]
    if spec.kinds:
        where.append(Fact.kind.in_(spec.kinds))
    if spec.since:
        where.append(Fact.at >= spec.since)
    if spec.until:
        where.append(Fact.at < spec.until)
    for key, value in spec.filters:
        where.append(Fact.fields[key].as_string() == value)

    if spec.aggregate == "last":
        return await _last(session, where, spec)

    group = Fact.fields[spec.group_by].as_string() if spec.group_by else None

    if spec.aggregate == "count":
        columns = [func.count(), func.count(), func.literal(0)]
    else:
        # Exact, not heuristic: coerce.to_json_value guarantees that
        # anything parseable was stored as a real JSON number, so the type
        # test is the whole answer to "will this cast blow up".
        numeric = func.jsonb_typeof(Fact.fields[spec.field]) == "number"
        value = Fact.fields[spec.field].as_float()
        aggregate = {
            "sum": func.sum,
            "balance_by": func.sum,
            "avg": func.avg,
            "min": func.min,
            "max": func.max,
        }[spec.aggregate]
        columns = [
            aggregate(value).filter(numeric),
            func.count().filter(numeric),
            func.count().filter(
                func.jsonb_typeof(Fact.fields[spec.field]).is_distinct_from("number")
            ),
        ]

    # Both branches select four columns — group, value, n, skipped — so
    # the row unpacking below has one shape.
    label = group if group is not None else cast(null(), String)
    statement = select(label, *columns).where(*where)
    if group is not None:
        statement = statement.group_by(group).order_by(func.count().desc())

    rows = (await session.execute(statement)).all()
    skipped = sum(int(row[3] or 0) for row in rows)
    return Answer(
        rows=[
            Row(
                group=row[0],
                value=float(row[1]) if row[1] is not None else None,
                n=int(row[2] or 0),
            )
            for row in rows
        ],
        skipped=skipped,
    )


async def _last(session: AsyncSession, where: list[Any], spec: Spec) -> Answer:
    """The most recent value, which is what a series question wants."""
    numeric = func.jsonb_typeof(Fact.fields[spec.field]) == "number"
    statement = (
        select(Fact.fields[spec.field].as_float())
        .where(*where, numeric)
        .order_by(Fact.at.desc())
        .limit(1)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return Answer(rows=[], skipped=0)
    return Answer(rows=[Row(group=None, value=float(row[0]), n=1)], skipped=0)
