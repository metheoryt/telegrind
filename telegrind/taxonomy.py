"""The vocabulary this chat's facts already use.

There is no registry of permitted kinds. What keeps the taxonomy from
exploding is that the extractor is shown what already exists and told to
reuse it — so this module is the thing standing between a free-form
`kind` and a hundred synonyms for "expense".
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import Fact


@dataclass(frozen=True, slots=True)
class KindUsage:
    """One observed kind, its field names, and how often it occurs."""

    kind: str
    fields: tuple[str, ...]
    count: int


async def observed(session: AsyncSession, chat_pk: int) -> list[KindUsage]:
    """Every live kind in this chat, most-used first.

    `jsonb_object_keys` is a set-returning function, so one row comes back
    per (kind, field) pair and the grouping happens here. The count on a
    kind is the count of its most common field — every fact has at least
    one, so that is the kind's own frequency.
    """
    field = func.jsonb_object_keys(Fact.fields).label("field")
    total = func.count().label("total")
    statement = (
        select(Fact.kind, field, total)
        .where(Fact.chat_pk == chat_pk, Fact.deleted_at.is_(None))
        .group_by(Fact.kind, field)
        .order_by(total.desc())
    )
    rows = (await session.execute(statement)).all()

    order: list[str] = []
    fields: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for kind, name, count in rows:
        if kind not in fields:
            order.append(kind)
            fields[kind] = []
            counts[kind] = count
        fields[kind].append(name)
        counts[kind] = max(counts[kind], count)

    # Sorted here, not left to the ORDER BY: the grouping above walks the
    # rows in arrival order, and a kind's own frequency only emerges once
    # all of its fields have been seen.
    return sorted(
        (
            KindUsage(kind=kind, fields=tuple(sorted(fields[kind])), count=counts[kind])
            for kind in order
        ),
        key=lambda u: -u.count,
    )


def render(usages: list[KindUsage]) -> str:
    """The taxonomy as it appears in a prompt."""
    if not usages:
        return "(в этом чате пока нет ни одного факта — заведи словарь сам)"
    return "\n".join(f"- {u.kind} ({u.count}): {', '.join(u.fields)}" for u in usages)
