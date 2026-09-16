"""The one-time comparison of v1's workbook against v2's facts.

The workbook never stored the text, so it cannot be an import source. It
stored what the OLD extractor made of each message, joined to the export
by message id at no cost — including every row the user corrected by
hand. That makes it a labelled set, and the only evidence available that
the export's ids are the ids v1 recorded.

Reads CSV files. gspread is not coming back.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegrind.models import Chat, Fact, LoggedMessage

log = logging.getLogger(__name__)


def message_id_of(key: str) -> int | None:
    """Column A: a bare `message_id`, or `<message_id>_<seq>` after 9540232."""
    head = key.strip().split("_", 1)[0]
    return int(head) if head.isdigit() else None


def read_sheet(path: Path) -> tuple[dict[str, dict], Counter]:
    """Rows by their full key, with ambiguous ones dropped and counted.

    Counted in one pass over the keys first, so that a key appearing
    three times drops all three rather than «the first one wins».
    """
    dropped: Counter = Counter()
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    keyed: list[tuple[str, dict]] = []
    for row in rows:
        key = str(next(iter(row.values())) or "").strip()
        if message_id_of(key) is None:
            dropped["unparsable"] += 1
            continue
        keyed.append((key, row))

    counts = Counter(key for key, _ in keyed)
    duplicate = sum(n for n in counts.values() if n > 1)
    # A clean sheet must leave `dropped` falsy — `Counter({"duplicate": 0})`
    # is truthy, and `report`'s callers rely on `not dropped` to mean
    # nothing was thrown away.
    if duplicate:
        dropped["duplicate"] = duplicate
    return {key: row for key, row in keyed if counts[key] == 1}, dropped


async def report(
    session: AsyncSession, chat_pk: int, sheets: dict[str, dict], out: Path
) -> Counter:
    """Write old rows beside new facts, message by message.

    Three outcomes per workbook row, and the second is the interesting
    one: matched, or the message is missing from the log — which means it
    was deleted from the chat and the workbook is its only remaining
    trace — or the message is there and produced no fact.
    """
    tally: Counter = Counter()
    lines: list[str] = []

    messages = {
        row.message_id: row
        for row in (
            await session.execute(
                select(LoggedMessage).where(LoggedMessage.chat_pk == chat_pk)
            )
        ).scalars()
    }
    facts: dict[int, list[Fact]] = {}
    for fact in (
        await session.execute(
            select(Fact).where(Fact.chat_pk == chat_pk, Fact.deleted_at.is_(None))
        )
    ).scalars():
        facts.setdefault(fact.message_pk, []).append(fact)

    for sheet, rows in sheets.items():
        for key, old in sorted(
            rows.items(), key=lambda kv: (message_id_of(kv[0]) or 0, kv[0])
        ):
            message_id = message_id_of(key)
            row = messages.get(message_id) if message_id is not None else None
            if row is None:
                tally["missing_message"] += 1
                lines.append(
                    f"{sheet}\t{key}\tMISSING\t{json.dumps(old, ensure_ascii=False)}\t"
                )
                continue
            new = facts.get(row.id, [])
            tally["matched" if new else "no_fact"] += 1
            new_dump = json.dumps(
                [{"kind": f.kind, **f.fields} for f in new], ensure_ascii=False
            )
            lines.append(
                f"{sheet}\t{key}\t{row.text!r}\t"
                f"{json.dumps(old, ensure_ascii=False)}\t{new_dump}"
            )

    out.write_text("\n".join(lines), encoding="utf-8")
    return tally


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m telegrind.workbook_compare")
    parser.add_argument(
        "csv", type=Path, nargs="+", help="one CSV export per worksheet"
    )
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    # Same reason as import_history.py: the row is read after its own
    # transaction commits, and the default would expire it into implicit IO
    # with no greenlet on the stack.
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        sheets: dict[str, dict] = {}
        for path in args.csv:
            rows, dropped = read_sheet(path)
            if dropped:
                log.info("%s: dropped %s", path.stem, dict(dropped))
            sheets[path.stem] = rows

        async with async_session() as session:
            async with session.begin():
                chat = (
                    await session.execute(
                        select(Chat).where(Chat.chat_id == args.chat_id)
                    )
                ).scalar_one()

            async with session.begin():
                tally = await report(session, chat.id, sheets, args.out)

            log.info("tally=%s", dict(tally))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    )
    asyncio.run(main())
