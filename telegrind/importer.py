"""One-time import of pre-bot worksheet history into the fact table.

The workbook is a projection of facts, and /rebuild rewrites each category's
declared column range. On day one the fact table is empty while the workbook
holds years of expenses, so a rebuild would write nothing over everything.
This module is what closes that gap.

Imported facts carry origin="imported" and no message: their source text was
never logged, so they are re-projectable but not re-extractable. /reparse
skips them (Phase 2).

One-time code, kept in its own module so it stays deletable.
"""

import logging
from dataclasses import dataclass

from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import ORIGIN_IMPORTED, Chat, Fact
from telegrind.registry import KEY_HEADER, Category, Registry
from telegrind.sheets import HeaderMismatchError, Worksheet
from telegrind.store import fact_keys_for_worksheet

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportedRow:
    sheet_key: str
    fields: dict[str, object]
    synthesized: bool


def synthesized_key(worksheet: str, row_no: int) -> str:
    """A key for a row that has none.

    Hand-added rows have no Telegram message_id in column A. Without a key
    they would stay permanently unaccounted and /rebuild would refuse
    forever, turning the safety gate into a dead end.
    """
    return f"import-{worksheet}-{row_no}"


def map_row(
    cat: Category,
    sheet_headers: list[str],
    row: list[str],
    row_no: int,
) -> ImportedRow:
    """Read one sheet row into a fact's fields, by header, not by position."""
    positions = {header: i for i, header in enumerate(sheet_headers)}

    def value(header: str) -> str:
        i = positions.get(header)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    key_index = positions.get(KEY_HEADER, 0)
    raw_key = (row[key_index] or "").strip() if key_index < len(row) else ""
    synthesized = not raw_key

    return ImportedRow(
        sheet_key=raw_key or synthesized_key(cat.worksheet, row_no),
        fields={c.header: value(c.header) for c in cat.columns},
        synthesized=synthesized,
    )


def plan_worksheet(cat: Category, values: list[list[str]]) -> list[ImportedRow]:
    """Every data row of one worksheet, as importable rows."""
    if len(values) <= 1:
        return []

    sheet_headers = [h.strip() for h in values[0]]
    rows: list[ImportedRow] = []
    seen: set[str] = set()

    for row_no, row in enumerate(values[1:], start=2):
        if not any((c or "").strip() for c in row):
            continue
        imported = map_row(cat, sheet_headers, row, row_no)
        if imported.sheet_key in seen:
            imported = ImportedRow(
                synthesized_key(cat.worksheet, row_no), imported.fields, True
            )
            log.warning(
                "%s row %s repeats key %r; importing it as %r",
                cat.worksheet,
                row_no,
                row[0],
                imported.sheet_key,
            )
        seen.add(imported.sheet_key)
        rows.append(imported)

    return rows


async def plan_import(
    ags: AsyncioGspreadSpreadsheet, registry: Registry
) -> tuple[dict[str, list[ImportedRow]], dict[str, str]]:
    """Read every declared worksheet. Returns `(plan, refused)`.

    A worksheet whose headers collide with the registry is skipped rather
    than imported: `map_row` reads by header, so importing it would mint
    facts with the wrong fields and the next projection would write them
    back as truth. One collided sheet must not abort the other categories,
    which is why this is per-worksheet and reported rather than raised.
    """
    plan: dict[str, list[ImportedRow]] = {}
    refused: dict[str, str] = {}
    for cat in registry.categories:
        ws = Worksheet(ags, cat.worksheet, cat.headers)
        try:
            values = await ws.all_values()
        except HeaderMismatchError as exc:
            log.warning("skipping %s on import: %s", cat.worksheet, exc)
            refused[cat.worksheet] = str(exc)
            continue
        rows = plan_worksheet(cat, values)
        if rows:
            plan[cat.name] = rows
    return plan, refused


async def apply_import(
    session: AsyncSession,
    chat: Chat,
    registry: Registry,
    plan: dict[str, list[ImportedRow]],
) -> tuple[int, int]:
    """Insert the plan as imported facts. Idempotent, so it is re-runnable.

    Returns `(imported, skipped)`.
    """
    imported = 0
    skipped = 0

    for category, rows in plan.items():
        cat = registry.by_name(category)
        if cat is None:
            continue
        existing = await fact_keys_for_worksheet(session, chat.id, cat.worksheet)
        for row in rows:
            if row.sheet_key in existing:
                skipped += 1
                continue
            session.add(
                Fact(
                    chat_pk=chat.id,
                    message_pk=None,
                    seq=1,
                    category=cat.name,
                    fields=row.fields,
                    origin=ORIGIN_IMPORTED,
                    model=None,
                    prompt_version=None,
                    worksheet=cat.worksheet,
                    sheet_key=row.sheet_key,
                )
            )
            existing.add(row.sheet_key)
            imported += 1

    await session.flush()
    return imported, skipped
