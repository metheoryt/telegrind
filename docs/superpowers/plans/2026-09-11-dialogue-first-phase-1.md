# Dialogue-first, Phase 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strip the spreadsheet out of telegrind and leave a bot that stores every message unconditionally, acknowledges it with a 💔 reaction, and tombstones that message's facts when the user taps the reaction back.

**Architecture:** The Google Sheets layer (`sheets.py`, `projection.py`, `registry.py`) is deleted outright. What it carried that is still needed — the user's timezone and default currency — moves onto the `chat` row. The `message` table gains extraction-state columns so a later batch pass can be idempotent; the `fact` table sheds its spreadsheet coordinates and becomes service columns plus JSONB. Ingest stops extracting and stops replying: it stores, marks whether the message is a candidate for extraction, and sets one reaction. A new `message_reaction` handler turns the user's tap into a soft delete.

**Tech Stack:** Python 3.14, aiogram 3.27, SQLAlchemy 2 (asyncpg), Alembic, pytest with `asyncio_mode = "auto"`, uv.

**Spec:** `docs/superpowers/specs/2026-09-11-dialogue-first-design.md`

## Global Constraints

- **Nothing written is ever lost.** Every handler writes the message row to
  Postgres unconditionally. Declining to extract is never licence to drop a
  message. This outranks every other consideration in this plan.
- **No echo.** No handler replies to an ordinary incoming message. The only
  outward signal on ingest is one `setMessageReaction`.
- **One reaction per message, both sides.** A bot setting two gets
  `REACTIONS_TOO_MANY`. The bot's own reaction generates no update;
  `old_reaction`/`new_reaction` carry one user's reactions, not the message
  total.
- **The delete emoji is 💔**, placed by the bot. *Any* user reaction deletes;
  removing the reaction restores.
- **Extraction is out of scope in this phase.** No LLM call happens anywhere in
  the request path. `llm.py` survives as plumbing plus the prompt-rules prose,
  and Phase 2 rewrites it.
- **`gspread-asyncio` and the Google service account are gone** and must not be
  reintroduced by any task here.
- Tests are pure unit tests: no live database, no network. Follow the existing
  convention in `tests/test_store.py` — `SimpleNamespace` fakes for aiogram
  objects, unattached SQLAlchemy model instances, hand-written fake sessions.
- Run tests with `uv run pytest`. Lint with `uv run ruff check` and
  `uv run ruff format`. Type-check with `uv run ty check`.

## Scope — this is Phase 1 of three

| Phase | Deliverable |
| --- | --- |
| **1 (this plan)** | Sheet layer deleted; store-only ingest; 💔 receipt; reaction-driven soft delete. |
| 2 | Batch extraction over a window with the observed taxonomy, and `/q`. |
| 3 | History import from the Telegram export, and the workbook-as-labelled-set comparison. |

Phase 1 is working software on its own: a bot you write into that keeps
everything and lets you take a message back.

## File Structure

**Deleted**

| Path | Why |
| --- | --- |
| `telegrind/sheets.py` | The workbook layer. `Config` is rehomed first (Task 2). |
| `telegrind/projection.py` | Writes sheet rows. Nothing writes sheet rows now. |
| `telegrind/registry.py` | The declared registry is replaced by an observed one in Phase 2. |
| `telegrind/bot/handlers/commands.py` | Every command in it (`/link`, `/unlink`, `/import`, `/rebuild`, `/reload`) is about the workbook. |
| `telegrind/importer.py` | The worksheet importer. Phase 3 writes a new one against the Telegram export. |
| `telegrind/bot/handlers/start.py` | The intro. `/start` now falls to the command handler and is stored like anything else; Phase 2's `/q` is where the bot speaks again. |
| `telegrind/bot/const.py` | `INTRO_TEXT`, `TIP_TEXT` and `SERVICE_ACCOUNT_EMAIL` — only `start.py` and `commands.py` read them. |
| `telegrind/services/` | Empty but for `__pycache__`. |
| `tests/test_sheets.py`, `tests/test_projection.py`, `tests/test_registry.py`, `tests/test_commands.py`, `tests/test_importer.py`, `tests/test_reply.py`, `tests/test_coerce.py` | Cover deleted code. `test_coerce.py` is re-created in Task 5 against the new coercion boundary. |

**Created**

| Path | Responsibility |
| --- | --- |
| `telegrind/config.py` | `ChatConfig` — timezone offset and default currency, read off the `chat` row. No I/O. |
| `telegrind/bot/handlers/reactions.py` | The `message_reaction` handler: tombstone and restore. |
| `tests/test_config.py`, `tests/test_coerce.py`, `tests/test_reactions.py`, `tests/test_ingest.py` | One test module per new unit. |

**Modified**

| Path | Change |
| --- | --- |
| `telegrind/models.py` | `chat` gains config columns; `message` gains extraction state; `fact` is reshaped. |
| `telegrind/store.py` | Drops the worksheet queries, gains extraction-state and tombstone helpers. |
| `telegrind/bot/handlers/handlers.py` | Ingest stops extracting, stops replying, sets the reaction. |
| `telegrind/bot/middleware.py` | Stops authorising Google, stops loading the registry, injects `ChatConfig`. |
| `telegrind/bot/setup.py` | Nothing to add — including the reactions module is enough. |
| `telegrind/llm.py` | Reduced to client plumbing plus `EXTRACTION_RULES`. |
| `telegrind/coerce.py` | Rewritten. `coerce_fields` goes with the registry; **`coerce_datetime`'s dateparser logic stays** — turning «вчера» into an instant is exactly what the `at` column needs — but it returns a `datetime` instead of a sheet-formatted string. |
| `main.py` | Drops the service-account credentials and the gspread client manager. |
| `pyproject.toml` | Drops `gspread-asyncio`; `google-auth` comes in transitively and goes with it. |
| `alembic/versions/*` | One new migration. |

---

### Task 1: Delete the sheet layer

Nothing here is additive. The point of doing it first is that the old shapes
stop being visible while the new ones are designed, and the diff is the record
of what was deliberately not carried forward.

**Files:**
- Delete: `telegrind/sheets.py`, `telegrind/projection.py`, `telegrind/registry.py`, `telegrind/bot/handlers/commands.py`
- Delete: `tests/test_sheets.py`, `tests/test_projection.py`, `tests/test_registry.py`, `tests/test_commands.py`, `tests/test_importer.py`, `tests/test_reply.py`, `tests/test_coerce.py`
- Modify: `telegrind/llm.py`, `telegrind/bot/handlers/handlers.py`, `telegrind/bot/middleware.py`, `main.py`, `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: a tree that imports and a test suite that passes, with no reference
  to gspread. `telegrind.llm.EXTRACTION_RULES: str` and
  `telegrind.llm.PROMPT_VERSION: str` survive for Phase 2.

- [x] **Step 1: Record what the deletion must preserve**

Before deleting `llm.py`'s registry-shaped parts, copy the prose rules out. Open
`telegrind/llm.py` and find `build_system_prompt`. The list of `Rules:` lines is
accumulated judgement about real messages — the loan sign convention, "a leading
amount is an expense, whatever follows it", date-of-fact versus mentioned-date.
It must survive verbatim as a module constant.

- [x] **Step 2: Reduce `telegrind/llm.py`**

Replace the whole file with the following. `build_schema`, `_branch`,
`_property_schema`, `build_system_prompt`, `build_user_message` and `extract`
all go — they are registry-shaped and Phase 2 rewrites them against the observed
taxonomy. The client plumbing, the version marker and the rules corpus stay.

```python
"""Anthropic plumbing and the extraction rules corpus.

Phase 1 makes no LLM call. What is kept here is what Phase 2 needs and
cannot re-derive: the client, the model names, and the rules prose, which
is accumulated judgement about real messages rather than code.
"""

import logging
import os

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-09-10.1"

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048

#: Kept verbatim from the registry-era system prompt. Phase 2 composes this
#: with the observed taxonomy; the judgement in it does not depend on how
#: the categories are declared, so it outlives the registry.
EXTRACTION_RULES = """\
- One message may hold several facts. Return one array element each.
- Return an empty array only for a message that states no fact at all.
- Anything you cannot confidently place goes to the `facts` category,
  with the message text kept verbatim. Never drop a fact.
- When a message opens with an amount of money and no other category
  fits it, it is an `expense`, and the rest of the message is the
  comment: `4500 такси`, `444 куколд`, `300 фигня`, and `444` on its
  own with an empty comment. Do not fall back to `facts` because the
  comment names nothing you recognise as buyable — what it was spent
  on is not your judgement to make.
- Loan amounts carry a sign convention: a loan given out is negative
  (-100), a repayment received is positive (+100). A bare amount with
  no direction stated means a loan given out, so -100.
- Do not invent fields. Do not invent values. An unstated text field
  is an empty string.
- Keep the user's own wording in text fields; do not translate it.
- A date the message mentions *about* the thing is not the date of the
  fact. `билеты на 15 октября` was bought now and the flight is on the
  15th; `оплатил квартиру за октябрь` was paid now. Date the fact to
  when it happened, put the mentioned date in a `due` field if the
  category has one, and otherwise keep it in the text field.
"""


def client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def current_model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)
```

- [x] **Step 3: Delete the modules and their tests**

```bash
git rm telegrind/sheets.py telegrind/projection.py telegrind/registry.py \
       telegrind/bot/handlers/commands.py
git rm -f tests/test_sheets.py tests/test_projection.py tests/test_registry.py \
          tests/test_commands.py tests/test_importer.py tests/test_reply.py \
          tests/test_coerce.py
git rm telegrind/importer.py telegrind/bot/handlers/start.py telegrind/bot/const.py
git rm -f tests/test_start.py
rm -rf telegrind/services
```

- [x] **Step 4: Cut the sheet plumbing out of `main.py`**

Replace `main.py` with:

```python
import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegrind.bot.setup import setup_dispatcher


async def main() -> None:
    dp = setup_dispatcher()
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    token = os.environ["BOT_TOKEN"]
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot, async_session=async_session)

    await engine.dispose()


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    )
    asyncio.run(main())
```

- [x] **Step 5: Cut the sheet plumbing out of the middleware**

Replace `telegrind/bot/middleware.py` with the version below. It no longer
authorises Google, opens a workbook, or loads a registry. `ChatConfig` does not
exist yet — Task 2 adds it — so for now the middleware injects only the session
and the chat, and Task 2 puts the config back.

```python
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.models import Chat

from .dispatcher import dp

log = logging.getLogger(__name__)


@dp.update.middleware()
async def populate_chat_data(
    handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
    event: Update,
    data: dict[str, Any],
) -> Any:
    if not isinstance(event.event, Message):
        return None
    msg: Message = event.event

    async_session: async_sessionmaker[AsyncSession] = data["async_session"]
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Chat).where(Chat.chat_id == msg.chat.id)
            )
            chat: Chat | None = result.scalar_one_or_none()
            if not chat:
                chat = Chat(chat_id=msg.chat.id)
                session.add(chat)

        data["chat"] = chat
        data["session"] = session

        return await handler(event, data)
```

Note the `isinstance(event.event, Message)` guard: Task 6 has to widen it, or
the reaction handler never runs. Leave it for now.

- [x] **Step 6: Strip the handlers down to what still imports**

In `telegrind/bot/handlers/handlers.py`, delete the imports of
`gspread_asyncio`, `telegrind.projection`, `telegrind.registry` and
`telegrind.sheets`, and delete `format_records`, `_ingest`, `delete_record`,
`escalate_stub`, and the `ags`/`registry`/`config` parameters of `record_text`
and `record_edited`. Task 5 rewrites this file properly; here the only goal is
that the package imports. The file should reduce to `is_marker`, `COMMAND_LIKE`,
the text constants, `record_voice`, `unknown_command`, and text/edited handlers
that do nothing but `store.upsert_message`.

- [x] **Step 7: Drop the dependency**

In `pyproject.toml`, remove the `"gspread-asyncio>=2.0.0",` line from
`[project] dependencies`. Then:

```bash
uv sync
```

- [x] **Step 8: Verify nothing references the deleted modules**

```bash
grep -rn "gspread\|projection\|registry\|sheets\|AsyncioGspread\|service_account" \
  telegrind/ main.py tests/ alembic/env.py
```

Expected: no hits. The word `registry` may still appear in a comment; a hit in
an `import` line is a failure.

- [x] **Step 9: Run the suite**

```bash
uv run pytest
```

Expected: PASS. The remaining modules are `test_harness.py`, `test_llm.py`,
`test_models.py`, `test_store.py`, `test_extraction_quality.py`.
`test_llm.py` and `test_extraction_quality.py` exercise the deleted
`build_schema`/`extract`; delete any test in them that names a removed symbol,
and keep whatever only asserts on `PROMPT_VERSION` or the client.

- [x] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor!: delete the workbook layer

The sheet stops being the product. sheets.py, projection.py, registry.py
and the workbook commands go; llm.py keeps the client and the extraction
rules corpus, which is judgement about real messages rather than code.

Phase 2 rewrites extraction against an observed taxonomy."
```

---

### Task 2: The chat's timezone and currency move onto the chat row

`Config` was defined in `sheets.py` and read from a `_config` worksheet, so
deleting the workbook orphaned the user's timezone — which every future `at`
value and every period query depends on. It becomes two columns on `chat`.

**Files:**
- Create: `telegrind/config.py`
- Create: `tests/test_config.py`
- Modify: `telegrind/models.py`, `telegrind/bot/middleware.py`

**Interfaces:**
- Consumes: `telegrind.models.Chat`.
- Produces:
  - `Chat.tz_offset: Mapped[int]` — hours east of UTC, default `6`.
  - `Chat.currency: Mapped[str]` — ISO 4217, default `"KZT"`.
  - `telegrind.config.ChatConfig` — frozen dataclass with `tz_offset: int`,
    `currency: str`, property `tz: timezone`, method
    `localized(dt: datetime) -> datetime`, classmethod
    `of(chat: Chat) -> ChatConfig`.
  - Handlers receive it as the keyword argument `config`.

- [x] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
from datetime import UTC, datetime, timedelta, timezone

from telegrind.config import ChatConfig
from telegrind.models import Chat


def test_defaults_are_almaty_and_tenge() -> None:
    cfg = ChatConfig.of(Chat(chat_id=1))
    assert cfg.tz_offset == 6
    assert cfg.currency == "KZT"


def test_tz_is_a_fixed_offset() -> None:
    assert ChatConfig(tz_offset=6, currency="KZT").tz == timezone(timedelta(hours=6))


def test_localized_moves_an_utc_instant_into_the_chat_offset() -> None:
    cfg = ChatConfig(tz_offset=6, currency="KZT")
    local = cfg.localized(datetime(2026, 9, 9, 15, 40, tzinfo=UTC))
    assert local.hour == 21
    assert local.utcoffset() == timedelta(hours=6)


def test_of_reads_what_the_row_carries() -> None:
    cfg = ChatConfig.of(Chat(chat_id=1, tz_offset=-5, currency="USD"))
    assert cfg.tz_offset == -5
    assert cfg.currency == "USD"
```

- [x] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'telegrind.config'`.

- [x] **Step 3: Add the columns**

In `telegrind/models.py`, inside `class Chat`, after `sheet_url`:

```python
    #: Hours east of UTC. A fixed offset, not a zone name: the bot serves one
    #: person per chat and DST has never come up. Default is Almaty.
    tz_offset: Mapped[int] = mapped_column(default=6, server_default="6")
    #: ISO 4217, used when a message states an amount and no currency.
    currency: Mapped[str] = mapped_column(default="KZT", server_default="KZT")
```

- [x] **Step 4: Write the implementation**

Create `telegrind/config.py`:

```python
"""Per-chat settings, read off the chat row.

These used to live in a `_config` worksheet. They are not spreadsheet
concerns: the timezone decides what date a fact gets and what "в августе"
means, and the currency is what an amount defaults to.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegrind.models import Chat


@dataclass(frozen=True, slots=True)
class ChatConfig:
    tz_offset: int
    currency: str

    @classmethod
    def of(cls, chat: Chat) -> "ChatConfig":
        return cls(tz_offset=chat.tz_offset, currency=chat.currency)

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.tz_offset))

    def localized(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)
```

`ChatConfig.of` reads the attribute rather than the column default, so an
unflushed `Chat(chat_id=1)` must already carry them. SQLAlchemy applies a
`mapped_column(default=...)` on flush, not on construction — so if
`test_defaults_are_almaty_and_tenge` fails with `None`, add the Python-side
defaults to the model as literal class attributes by declaring them
`Mapped[int] = mapped_column(default=6, server_default="6", insert_default=6)`
and construct with `Chat(chat_id=1, tz_offset=6, currency="KZT")` in the test
instead. Prefer changing the test: the production path always reads a row that
has been through the database.

- [x] **Step 5: Run the test to verify it passes**

```bash
uv run pytest tests/test_config.py -v
```

Expected: PASS.

- [x] **Step 6: Inject it from the middleware**

In `telegrind/bot/middleware.py`, add the import and the injection:

```python
from telegrind.config import ChatConfig
```

and, next to `data["chat"] = chat`:

```python
        data["config"] = ChatConfig.of(chat)
```

- [x] **Step 7: Run the whole suite**

```bash
uv run pytest
```

Expected: PASS.

- [x] **Step 8: Commit**

```bash
git add telegrind/config.py telegrind/models.py telegrind/bot/middleware.py tests/test_config.py
git commit -m "feat: the chat's timezone and currency live on the chat row

They were read from a _config worksheet, which the workbook deletion
took with it. Neither is a spreadsheet concern: the offset decides what
date a fact gets, and the currency is what a bare amount defaults to."
```

---

### Task 3: Extraction state on `message`, and `fact` reshaped

**Files:**
- Modify: `telegrind/models.py`, `tests/test_models.py`
- Create: `alembic/versions/<generated>_dialogue_first.py`

**Interfaces:**
- Consumes: Task 2's `Chat` columns (the same migration carries them).
- Produces:
  - `LoggedMessage.extracted_at: Mapped[datetime | None]`,
    `.extract_model: Mapped[str | None]`,
    `.extract_prompt_version: Mapped[str | None]`,
    `.extractable: Mapped[bool]` (default `True`),
    `.extract_error: Mapped[str | None]`.
  - `Fact` with `chat_pk`, `message_pk` (not null), `seq`, `kind`, `at`,
    `fields`, `model`, `prompt_version`, `created_at`, `updated_at`,
    `deleted_at`. No `worksheet`, no `sheet_key`, no `origin`, no `category`.

**This migration destroys the existing `fact` rows.** Facts are a derivation of
messages and are re-derivable; messages are not touched. Prod currently holds
about two days of facts, and they come back on the first `/q` in Phase 2. Say so
in the migration's docstring so nobody mistakes it for an accident.

- [x] **Step 1: Write the failing test**

Replace the `Fact`-related tests in `tests/test_models.py` and add the message
ones. Keep `test_table_names` and `test_telegram_ids_are_bigints` as they are.

```python
from datetime import UTC, datetime

from telegrind.models import Chat, Fact, LoggedMessage


def test_message_carries_extraction_state() -> None:
    columns = set(LoggedMessage.__table__.columns.keys())
    assert {
        "extracted_at",
        "extract_model",
        "extract_prompt_version",
        "extractable",
        "extract_error",
    } <= columns


def test_a_new_message_is_extractable_and_unextracted() -> None:
    row = LoggedMessage(chat_pk=1, message_id=2, kind="text", raw={})
    assert row.extracted_at is None
    assert row.extract_error is None


def test_fact_has_no_spreadsheet_coordinates() -> None:
    columns = set(Fact.__table__.columns.keys())
    assert "worksheet" not in columns
    assert "sheet_key" not in columns
    assert "origin" not in columns
    assert "category" not in columns


def test_fact_is_service_columns_plus_jsonb() -> None:
    columns = set(Fact.__table__.columns.keys())
    assert {
        "chat_pk",
        "message_pk",
        "seq",
        "kind",
        "at",
        "fields",
        "created_at",
        "updated_at",
        "deleted_at",
    } <= columns


def test_fact_requires_a_source_message() -> None:
    assert Fact.__table__.c.message_pk.nullable is False


def test_fact_at_is_timezone_aware() -> None:
    assert Fact.__table__.c.at.type.timezone is True


def test_chat_carries_its_own_settings() -> None:
    assert {"tz_offset", "currency"} <= set(Chat.__table__.columns.keys())


def test_a_fact_can_hold_a_real_json_number() -> None:
    fact = Fact(
        chat_pk=1,
        message_pk=1,
        seq=1,
        kind="expense",
        at=datetime(2026, 9, 9, tzinfo=UTC),
        fields={"amount": 4500, "currency": "KZT", "comment": "такси"},
    )
    assert isinstance(fact.fields["amount"], int)
```

- [x] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_models.py -v
```

Expected: FAIL — `extracted_at` is not a column, and `worksheet` still is.

- [x] **Step 3: Rewrite the two models**

In `telegrind/models.py`, delete the `ORIGIN_EXTRACTED` / `ORIGIN_IMPORTED`
constants. Add to `LoggedMessage`, after `created_at`:

```python
    #: When the batch pass last extracted this message. Null means it has
    #: not been extracted yet — which is NOT the same as "extracted and
    #: yielded nothing", and that difference is why this column exists.
    extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    extract_model: Mapped[str | None] = mapped_column(default=None)
    extract_prompt_version: Mapped[str | None] = mapped_column(default=None)
    #: False for /q, for commands, and for imported bot replies. Such a
    #: message is stored like any other — nothing written is ever lost —
    #: but it must never reach the extractor, or the batch pass coins a
    #: kind out of a question and poisons the observed taxonomy.
    extractable: Mapped[bool] = mapped_column(default=True, server_default="true")
    #: The last extraction failure. Without it, dropping the echo would
    #: make a failed extraction completely silent.
    extract_error: Mapped[str | None] = mapped_column(default=None)
```

Replace the whole `Fact` class with:

```python
class Fact(Model):
    """A replaceable derivation of a LoggedMessage.

    Service columns plus JSONB. Only `kind` and `at` are promoted out of
    `fields`, because every query filters on both. The numeric shape is
    deliberately not promoted: expenses are flows, measurements are levels,
    habits have no number, assets have a balance. Promoting later is a
    generated column, not a rewrite.
    """

    __tablename__ = "fact"
    __table_args__ = (
        Index(
            "uq_fact_message_pk_seq_live",
            "message_pk",
            "seq",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_fact_chat_kind_at_live",
            "chat_pk",
            "kind",
            "at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_fact_fields", "fields", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    message_pk: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE")
    )
    #: 1-based position within the message.
    seq: Mapped[int]
    #: Free-form, coined by the model and reused through the observed
    #: taxonomy. There is no registry of permitted values.
    kind: Mapped[str]
    #: When the fact HAPPENED, which is not created_at. Falls back to the
    #: message's tg_date when the text states no time of its own.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Everything else. Numbers in here are real JSON numbers — see
    #: telegrind/coerce.py, which is the only place that writes them.
    fields: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(default=None)
    prompt_version: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    #: A tombstone, and not for undo. Facts are re-derivable, so a hard
    #: delete is undone by the next re-extraction of the same message.
    #: Only an explicit un-delete by the user clears this.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

Add `Index` and `text` to the `sqlalchemy` import at the top of the file:

```python
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
    text,
)
```

The uniqueness is a partial unique *index*, not a `UniqueConstraint`: a
tombstoned fact keeps its `(message_pk, seq)`, so a total constraint would make
re-extracting an edited message collide with the row it replaces.

- [x] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_models.py -v
```

Expected: PASS.

- [x] **Step 5: Generate the migration**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "dialogue first"
```

- [x] **Step 6: Fix the generated migration by hand**

Autogenerate will emit `add_column`/`drop_column` for `fact`. Replace the `fact`
part with a drop-and-recreate, and write the docstring that explains it. The
file should read:

```python
"""dialogue first

The workbook is gone. `message` gains extraction state so a deferred batch
pass can be idempotent, `chat` gains the settings the `_config` worksheet
used to hold, and `fact` is recreated without its spreadsheet coordinates.

DESTRUCTIVE, DELIBERATELY: this drops every existing `fact` row. Facts are
a derivation of messages and come back on the first extraction pass. The
`message` table — the thing the product promises never to lose — is not
touched.
"""
```

Then, in `upgrade()`:

```python
def upgrade() -> None:
    op.add_column("chat", sa.Column("tz_offset", sa.Integer(), nullable=False,
                                    server_default="6"))
    op.add_column("chat", sa.Column("currency", sa.String(), nullable=False,
                                    server_default="KZT"))

    op.add_column("message", sa.Column("extracted_at", sa.DateTime(timezone=True),
                                       nullable=True))
    op.add_column("message", sa.Column("extract_model", sa.String(), nullable=True))
    op.add_column("message", sa.Column("extract_prompt_version", sa.String(),
                                       nullable=True))
    op.add_column("message", sa.Column("extractable", sa.Boolean(), nullable=False,
                                       server_default="true"))
    op.add_column("message", sa.Column("extract_error", sa.String(), nullable=True))

    op.drop_table("fact")
    op.create_table(
        "fact",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_pk", sa.Integer(),
                  sa.ForeignKey("chat.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_pk", sa.Integer(),
                  sa.ForeignKey("message.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_fact_message_pk_seq_live", "fact", ["message_pk", "seq"],
                    unique=True, postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_fact_chat_kind_at_live", "fact", ["chat_pk", "kind", "at"],
                    postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_fact_fields", "fact", ["fields"], postgresql_using="gin")
```

`downgrade()` recreates the old `fact` shape empty and drops the added columns.
It cannot restore the dropped rows; say so in a comment rather than pretending.

Add `from sqlalchemy.dialects import postgresql` to the migration's imports.

- [x] **Step 7: Verify the migration round-trips**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: all three succeed with no error.

- [x] **Step 8: Run the whole suite**

```bash
uv run pytest && uv run ruff check && uv run ty check
```

Expected: PASS. `alembic/versions` is excluded from `ty`; if ruff trips on the
generated file, run `uv run ruff format alembic/versions/` first.

- [x] **Step 9: Commit**

```bash
git add telegrind/models.py tests/test_models.py alembic/versions/
git commit -m "feat!: extraction state on message, fact without the sheet

A deferred batch pass cannot tell 'not yet parsed' from 'parsed, yielded
nothing' unless the message row says so, so extracted_at is a column and
not an inference from having fact rows.

fact is recreated as service columns plus JSONB. Only kind and at are
promoted: every query filters on both. The numeric shape stays in JSONB
because expenses are flows, measurements are levels and habits have no
number — promoting later is a generated column.

The existing fact rows are dropped. They are re-derivable; messages are
untouched."
```

---

### Task 4: Store helpers for extraction state and tombstones

**Files:**
- Modify: `telegrind/store.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Consumes: Task 3's models.
- Produces:
  - `store.mark_extractable(values: dict, extractable: bool) -> dict` is *not*
    added; `extractable` is passed through `upsert_message`.
  - `async store.upsert_message(session, chat, msg, *, extractable: bool = True) -> tuple[LoggedMessage, bool]`
  - `async store.live_facts_for_message(session, message_pk: int) -> list[Fact]`
  - `async store.tombstone_facts(session, message_pk: int, at: datetime) -> int`
  - `async store.restore_facts(session, message_pk: int) -> int`
  - `facts_for_chat` loses its `worksheet` parameter;
    `fact_keys_for_worksheet` is deleted.

- [x] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
from datetime import UTC, datetime

from telegrind.models import Fact
from telegrind.store import message_values, tombstone_facts


class FakeResult:
    def __init__(self, rows: list[Fact]) -> None:
        self._rows = rows

    def scalars(self) -> list[Fact]:
        return self._rows


class FakeSession:
    """Enough of AsyncSession for the tombstone helpers.

    They only ever select and mutate attributes — nothing here flushes."""

    def __init__(self, rows: list[Fact]) -> None:
        self.rows = rows
        self.queries: list[object] = []

    async def execute(self, query: object) -> FakeResult:
        self.queries.append(query)
        return FakeResult(self.rows)


def live(seq: int) -> Fact:
    return Fact(
        chat_pk=1,
        message_pk=7,
        seq=seq,
        kind="expense",
        at=datetime(2026, 9, 9, tzinfo=UTC),
        fields={"amount": 100},
    )


async def test_tombstone_stamps_every_live_fact() -> None:
    rows = [live(1), live(2)]
    session = FakeSession(rows)
    when = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    count = await tombstone_facts(session, message_pk=7, at=when)

    assert count == 2
    assert [r.deleted_at for r in rows] == [when, when]


async def test_tombstone_on_a_message_with_no_facts_reports_zero() -> None:
    session = FakeSession([])
    count = await tombstone_facts(session, message_pk=7,
                                  at=datetime(2026, 9, 11, tzinfo=UTC))
    assert count == 0


def test_values_do_not_carry_extractability() -> None:
    """extractable is a handler decision, not something lifted off the message."""
    assert "extractable" not in message_values(text_message())
```

- [x] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_store.py -v
```

Expected: FAIL with `ImportError: cannot import name 'tombstone_facts'`.

- [x] **Step 3: Write the implementation**

In `telegrind/store.py`, delete `fact_keys_for_worksheet`, drop the `worksheet`
parameter from `facts_for_chat`, and add:

```python
async def live_facts_for_message(
    session: AsyncSession, message_pk: int
) -> list[Fact]:
    """This message's facts that are not tombstoned."""
    result = await session.execute(
        select(Fact)
        .where(Fact.message_pk == message_pk, Fact.deleted_at.is_(None))
        .order_by(Fact.seq)
    )
    return list(result.scalars())


async def tombstone_facts(
    session: AsyncSession, message_pk: int, at: datetime
) -> int:
    """Soft-delete this message's live facts. Returns how many were stamped.

    A tombstone is never lifted by an extraction pass — only restore_facts
    clears it — because facts are re-derivable and a hard delete would be
    undone by the next re-extraction of the same message.
    """
    result = await session.execute(
        select(Fact).where(Fact.message_pk == message_pk, Fact.deleted_at.is_(None))
    )
    rows = list(result.scalars())
    for row in rows:
        row.deleted_at = at
    return len(rows)


async def restore_facts(session: AsyncSession, message_pk: int) -> int:
    """Clear the tombstone on this message's facts. Returns how many."""
    result = await session.execute(
        select(Fact).where(
            Fact.message_pk == message_pk, Fact.deleted_at.is_not(None)
        )
    )
    rows = list(result.scalars())
    for row in rows:
        row.deleted_at = None
    return len(rows)
```

Add `from datetime import datetime` at the top of the file.

Change `upsert_message`'s signature and body:

```python
async def upsert_message(
    session: AsyncSession,
    chat: Chat,
    msg: Message,
    *,
    extractable: bool = True,
) -> tuple[LoggedMessage, bool]:
    """Append the message, or overwrite it if we have seen this id before.

    Returns `(row, created)`. An edit overwrites the text, bumps edited_at,
    and clears the extraction state: the text changed, so whatever was
    extracted from it no longer describes the message.
    """
    values = message_values(msg)
    existing = await get_message(session, chat.id, msg.message_id)
    if existing is not None:
        for key, value in values.items():
            if key in ("transcript", "transcript_model") and value is None:
                continue
            setattr(existing, key, value)
        existing.extractable = extractable
        existing.extracted_at = None
        existing.extract_error = None
        return existing, False

    row = LoggedMessage(chat_pk=chat.id, extractable=extractable, **values)
    session.add(row)
    await session.flush()
    return row, True
```

Clearing `extracted_at` on an edit is what makes the next batch pass pick the
message up again. Task 5's edit handler relies on it.

- [x] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_store.py -v
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add telegrind/store.py tests/test_store.py
git commit -m "feat: store helpers for extraction state and tombstones

An edit clears extracted_at, which is what makes the next batch pass pick
the message up again. Tombstones are only ever lifted explicitly."
```

---

### Task 5: Ingest stores, marks, reacts — and says nothing

**Files:**
- Modify: `telegrind/bot/handlers/handlers.py`
- Rewrite: `telegrind/coerce.py`
- Create: `tests/test_coerce.py`, `tests/test_ingest.py`

**Interfaces:**
- Consumes: `store.upsert_message(..., extractable=...)` from Task 4,
  `ChatConfig` from Task 2.
- Produces:
  - `telegrind.coerce.to_json_value(raw: object) -> object` — the write
    boundary for a fact field.
  - `RECEIPT_EMOJI = "💔"` in `telegrind/bot/handlers/handlers.py`.
  - `async handlers.acknowledge(bot: Bot, chat_id: int, message_id: int) -> None`

`coerce.py` already exists and is registry-shaped: `coerce_fields` walks a
`Category`'s declared columns, and `projection.py` was its only caller, so it
goes with Task 1. What must **not** go is `coerce_datetime` — ISO first, then
dateparser with `languages=["ru", "en"]` and a `PREFER_DATES_FROM` that
separates a due date from an event date, then the message's own timestamp as
the fallback. That is precisely how a fact gets its `at`, and it is not
re-derivable from the spec. This task rewrites the module around it.

The rest is written here rather than in Phase 2 because number coercion is the
invariant the JSONB decision rests on, and Phase 2 should find it enforced.

- [x] **Step 1: Write the failing coercion test**

Create `tests/test_coerce.py`:

```python
from telegrind.coerce import to_json_value


def test_an_integer_string_becomes_a_json_number() -> None:
    assert to_json_value("4500") == 4500
    assert isinstance(to_json_value("4500"), int)


def test_a_decimal_string_becomes_a_float() -> None:
    assert to_json_value("12.5") == 12.5


def test_a_number_passes_through() -> None:
    assert to_json_value(4500) == 4500
    assert to_json_value(12.5) == 12.5


def test_spaced_thousands_do_not_become_a_number() -> None:
    """`5 000` is ambiguous — two numbers or one? Keep the user's text."""
    assert to_json_value("5 000") == "5 000"


def test_prose_stays_prose() -> None:
    assert to_json_value("около 500") == "около 500"
    assert to_json_value("много") == "много"


def test_none_stays_none() -> None:
    assert to_json_value(None) is None


def test_a_bool_is_not_coerced_to_a_number() -> None:
    assert to_json_value(True) is True


def test_an_iso_string_becomes_that_instant() -> None:
    cfg = ChatConfig(tz_offset=6, currency="KZT")
    fallback = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)
    assert to_instant("2026-08-01T12:00:00+06:00", cfg, fallback).month == 8


def test_a_relative_russian_date_resolves_against_the_message() -> None:
    cfg = ChatConfig(tz_offset=6, currency="KZT")
    fallback = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)
    assert to_instant("вчера", cfg, fallback).date().day == 8


def test_an_unparseable_date_falls_back_to_the_message_timestamp() -> None:
    cfg = ChatConfig(tz_offset=6, currency="KZT")
    fallback = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)
    assert to_instant("когда-нибудь", cfg, fallback) == fallback


def test_a_naive_parse_gets_the_chat_offset() -> None:
    cfg = ChatConfig(tz_offset=6, currency="KZT")
    fallback = datetime(2026, 9, 9, 15, 40, tzinfo=UTC)
    assert to_instant("", cfg, fallback).tzinfo is not None
```

The test module needs `from datetime import UTC, datetime`,
`from telegrind.config import ChatConfig` and `to_instant` in the import
from `telegrind.coerce`.

- [x] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_coerce.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'telegrind.coerce'`.

- [x] **Step 3: Write the coercion implementation**

Replace `telegrind/coerce.py` with the following. `coerce_fields`,
`coerce_number` and `coerce_currency` are registry-shaped and go; the dateparser
logic is kept and re-pointed at `ChatConfig`, returning a `datetime` rather than
a `"%d.%m.%y %H:%M"` string, because `Fact.at` is a real `timestamptz` now.

```python
"""The write boundary for fact field values.

Everything a fact knows lives in a JSONB column, so aggregation reads it
back with an expression like `(fields->>'amount')::numeric`. That does not
fail one row, it fails the whole query — and adding a generated column
later fails on the first non-numeric value in the table. So a value is
coerced once, here, on the way in: what parses becomes a real JSON number,
what does not stays text and simply never aggregates.

Nothing written is ever lost. That includes a number the model could not
pin down.
"""


def to_json_value(raw: object) -> object:
    """Return a JSON-safe value, with numbers as real numbers."""
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw
    if not isinstance(raw, str):
        return raw

    text = raw.strip()
    if not text:
        return raw
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return raw


def to_instant(
    raw: object,
    cfg: ChatConfig,
    fallback: datetime,
    *,
    prefer_future: bool = False,
) -> datetime:
    """ISO first, then dateparser, then the message's own timestamp.

    This is how a fact gets its `at`. `prefer_future` is what separates a
    due date from an event date: an ambiguous «во вторник» resolves forward
    for the first and backward for the second.
    """
    parsed: datetime | None = None

    if raw not in (None, ""):
        text = str(raw).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = dateparser.parse(
                text,
                languages=["ru", "en"],
                settings={
                    "TIMEZONE": f"{cfg.tz_offset:+03d}00",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future" if prefer_future else "past",
                    "RELATIVE_BASE": fallback.replace(tzinfo=None),
                },
            )

    if parsed is None:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=cfg.tz)
    return parsed
```

The module needs these imports at the top:

```python
from datetime import datetime

import dateparser

from telegrind.config import ChatConfig
```

`bool` is checked before `int` because `isinstance(True, int)` is true in
Python and a boolean must not be silently stored as `1`.

- [x] **Step 4: Run the coercion test to verify it passes**

```bash
uv run pytest tests/test_coerce.py -v
```

Expected: PASS.

- [x] **Step 5: Write the failing ingest test**

Create `tests/test_ingest.py`:

```python
from datetime import UTC, datetime
from types import SimpleNamespace

from telegrind.bot.handlers.handlers import RECEIPT_EMOJI, acknowledge


class FakeBot:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, int, list[str]]] = []

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.reactions.append(
            (chat_id, message_id, [r.emoji for r in reaction])
        )


def message(message_id: int = 4821) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 9, 9, 15, 40, tzinfo=UTC),
        edit_date=None,
        text="4500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        chat=SimpleNamespace(id=3260987),
        model_dump=lambda mode=None: {"message_id": message_id},
    )


async def test_the_receipt_is_a_broken_heart() -> None:
    assert RECEIPT_EMOJI == "💔"


async def test_acknowledge_sets_exactly_one_reaction() -> None:
    bot = FakeBot()
    await acknowledge(bot, chat_id=3260987, message_id=4821)
    assert bot.reactions == [(3260987, 4821, ["💔"])]


async def test_acknowledge_survives_a_telegram_failure() -> None:
    """The receipt is cosmetic; the message row is already committed."""

    class Failing(FakeBot):
        async def set_message_reaction(self, chat_id, message_id, reaction):
            raise RuntimeError("Bad Request: REACTION_INVALID")

    await acknowledge(Failing(), chat_id=1, message_id=2)
```

- [x] **Step 6: Run it to verify it fails**

```bash
uv run pytest tests/test_ingest.py -v
```

Expected: FAIL with `ImportError: cannot import name 'RECEIPT_EMOJI'`.

- [x] **Step 7: Rewrite the handlers**

Replace `telegrind/bot/handlers/handlers.py` with:

```python
"""Ingestion. Store, mark, react — and say nothing.

There is no echo. Writing a message produces no reply: the confirmation
that the bot understood arrives when you ask, in the answer to /q. What
the bot does say on ingest is one reaction, and that reaction is also the
delete affordance — see handlers/reactions.py.

Registration order is match order: /q first so it is never recorded as a
fact, then voice, then the slash catch-all, then plain text.
"""

import logging

from aiogram import Bot, F
from aiogram.types import Message, ReactionTypeEmoji
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.router import router
from telegrind.models import Chat

log = logging.getLogger(__name__)

#: What the bot puts on every stored message. The bubble's presence is the
#: receipt; its emoji names what tapping it does, because tapping it is the
#: delete gesture. A broken heart warns without 👎's flavour of the bot
#: disapproving of every line the user writes.
RECEIPT_EMOJI = "💔"

#: A slash-prefixed message. Stored like everything else, never extracted:
#: a batch pass must not coin a kind out of a command.
COMMAND_LIKE = F.text.startswith("/")


async def acknowledge(bot: Bot, chat_id: int, message_id: int) -> None:
    """Place the receipt reaction. Never fatal.

    The message row is committed before this runs, so a Telegram failure
    here costs a visual cue and nothing else. Raising would lose the
    update; the invariant is about the row, not the bubble.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=RECEIPT_EMOJI)],
        )
    except Exception:  # noqa: BLE001 — cosmetic, and the row is already safe
        log.warning("could not set the receipt reaction on %s", message_id)


async def _store(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    bot: Bot,
    *,
    extractable: bool,
) -> None:
    async with session.begin():
        await store.upsert_message(session, chat, message, extractable=extractable)
    await acknowledge(bot, message.chat.id, message.message_id)


@router.message(COMMAND_LIKE)
async def record_command(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Store a command without extracting it.

    /q is answered in Phase 2 and reaches this handler until then. Storing
    it keeps the invariant; extractable=False keeps it out of the taxonomy.
    """
    await _store(message, chat, session, bot, extractable=False)


@router.message(F.voice)
async def record_voice(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """Log the voice note without transcribing it.

    There is no ASR. Logging it now means a later transcription pass can
    reach back over everything recorded in the meantime.
    """
    await _store(message, chat, session, bot, extractable=True)


@router.message()
async def record_text(
    message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    await _store(message, chat, session, bot, extractable=True)


@router.edited_message()
async def record_edited(
    edited_message: Message, chat: Chat, session: AsyncSession, bot: Bot
) -> None:
    """An edit overwrites the text and clears the extraction state.

    upsert_message does the clearing, so the next batch pass picks the
    message up again. Nothing is re-extracted here: extraction happens
    when you ask.
    """
    await _store(edited_message, chat, session, bot, extractable=True)
```

`@router.message()` with no filter is deliberate: a sticker, a photo or a
document is a message the user sent, so it is stored. `message_values` already
handles a caption and a missing text.

- [x] **Step 8: Run the ingest test to verify it passes**

```bash
uv run pytest tests/test_ingest.py -v
```

Expected: PASS.

- [x] **Step 9: Run the whole suite**

```bash
uv run pytest && uv run ruff check && uv run ty check
```

Expected: PASS.

- [x] **Step 10: Commit**

```bash
git add -A
git commit -m "feat!: ingest stores, marks and reacts — and says nothing

No handler replies to an ordinary message any more. The only outward
signal is one reaction, which doubles as the delete affordance.

coerce.to_json_value is the write boundary the JSONB decision rests on:
what parses becomes a real JSON number, what does not stays text and
never aggregates, and the fact survives either way."
```

---

### Task 6: The tap is the delete

**Files:**
- Create: `telegrind/bot/handlers/reactions.py`, `tests/test_reactions.py`
- Modify: `telegrind/bot/setup.py`, `telegrind/bot/middleware.py`

**Interfaces:**
- Consumes: `store.tombstone_facts`, `store.restore_facts` from Task 4.
- Produces: a `message_reaction` observer. Registering it is what subscribes
  the update type — aiogram derives `allowed_updates` from the observers that
  have handlers, so there is no configuration to add.

- [x] **Step 1: Write the failing test**

Create `tests/test_reactions.py`:

```python
from datetime import UTC, datetime
from types import SimpleNamespace

from telegrind.bot.handlers.reactions import wants_delete


def event(old: list[str], new: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=3260987, type="private"),
        message_id=1072,
        date=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        old_reaction=[SimpleNamespace(type="emoji", emoji=e) for e in old],
        new_reaction=[SimpleNamespace(type="emoji", emoji=e) for e in new],
        user=SimpleNamespace(id=3260987, username="cyphy"),
    )


def test_adding_any_reaction_deletes() -> None:
    assert wants_delete(event([], ["💔"])) is True
    assert wants_delete(event([], ["👍"])) is True


def test_removing_the_reaction_restores() -> None:
    assert wants_delete(event(["💔"], [])) is False


def test_swapping_one_reaction_for_another_still_deletes() -> None:
    assert wants_delete(event(["💔"], ["👍"])) is True
```

- [x] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_reactions.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [x] **Step 3: Write the implementation**

Create `telegrind/bot/handlers/reactions.py`:

```python
"""The user's reaction is the delete gesture.

The bot has already put 💔 on the message, so the user taps that existing
bubble — one tap, no picker. Any reaction deletes, because the Bot API
cannot narrow which emoji a private chat offers: setMessageReaction sets
the *bot's* reaction and available_reactions is read-only. Since the
picker cannot be narrowed, the accepted set is widened instead.

Measured 2026-09-11: message_reaction does reach a bot in a private chat
despite the docs' "must be an administrator", the bot's own reaction
generates no update, and old_reaction/new_reaction carry this one user's
reactions rather than the message total — so nothing here has to work out
whose reaction it is looking at.
"""

import logging

from aiogram.types import MessageReactionUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.router import router
from telegrind.models import Chat

log = logging.getLogger(__name__)


def wants_delete(event: MessageReactionUpdated) -> bool:
    """True when the user now has a reaction on the message."""
    return bool(event.new_reaction)


@router.message_reaction()
async def toggle_delete(
    event: MessageReactionUpdated, chat: Chat, session: AsyncSession
) -> None:
    """Tombstone the message's facts, or lift the tombstone."""
    async with session.begin():
        row = await store.get_message(session, chat.id, event.message_id)
        if row is None:
            log.info("reaction on unknown message %s", event.message_id)
            return
        if wants_delete(event):
            count = await store.tombstone_facts(session, row.id, event.date)
            log.info("tombstoned %s fact(s) of message %s", count, event.message_id)
        else:
            count = await store.restore_facts(session, row.id)
            log.info("restored %s fact(s) of message %s", count, event.message_id)
```

There is no reply and no counter-reaction: the bubble the user just tapped is
already the visible state.

- [x] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_reactions.py -v
```

Expected: PASS.

- [x] **Step 5: Widen the middleware so the update reaches the handler**

`populate_chat_data` returns `None` for anything that is not a `Message`, which
silently drops every reaction update. In `telegrind/bot/middleware.py`, replace
the guard and the chat lookup:

```python
from aiogram.types import Message, MessageReactionUpdated, Update
```

```python
    event_object = event.event
    if isinstance(event_object, Message):
        chat_id = event_object.chat.id
    elif isinstance(event_object, MessageReactionUpdated):
        chat_id = event_object.chat.id
    else:
        return None
```

and use `chat_id` in the `select(Chat).where(Chat.chat_id == chat_id)`.

- [x] **Step 6: Register the module**

In `telegrind/bot/setup.py`, import it alongside the others:

```python
def setup_dispatcher() -> Dispatcher:
    from . import handlers  # noqa
    from . import middleware  # noqa
    from .handlers import reactions  # noqa

    dp.include_router(router)

    return dp
```

`ChatActionMiddleware` goes with the echo — nothing types any more.

- [x] **Step 7: Verify the update type is subscribed**

```bash
uv run python -c "
from telegrind.bot.setup import setup_dispatcher
print(sorted(setup_dispatcher().resolve_used_update_types()))
"
```

Expected: `['edited_message', 'message', 'message_reaction']`. If
`message_reaction` is absent, the handler is not registered and the probe result
does not save you — aiogram derives `allowed_updates` from the observers.

- [x] **Step 8: Run the whole suite**

```bash
uv run pytest && uv run ruff check && uv run ty check
```

Expected: PASS.

- [x] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: tapping the receipt reaction deletes the message's facts

Any user reaction tombstones, removing it restores. The middleware had to
widen: it returned None for every non-Message update, which silently
dropped reactions.

Registering the observer is what subscribes the update type — aiogram
derives allowed_updates from the handlers that exist."
```

---

### Task 7: End-to-end on the dev stack

Phase 1 has no automated integration test — there is no live-database fixture in
this repo and adding one is not this phase's job. The gate is a manual run.

**Files:** none.

- [ ] **Step 1: Bring the stack up**

```bash
docker compose up -d --build
docker compose logs -f bot
```

- [ ] **Step 2: Walk the happy path in the chat with the bot**

| Action | Expected |
| --- | --- |
| Send `4500 такси` | No reply. A 💔 appears on your message. |
| Send a voice note | No reply. 💔 appears. |
| Send `/help` | No reply. 💔 appears. |
| Edit `4500 такси` to `5500 такси` | 💔 stays. |
| Tap the 💔 | Nothing visible beyond your own reaction; the log says `tombstoned 0 fact(s)` — there are no facts yet in Phase 1. |
| Tap it again to remove | The log says `restored 0 fact(s)`. |

- [ ] **Step 3: Verify the rows landed**

```bash
docker compose exec postgres psql -U postgres -c \
  "select message_id, kind, extractable, extracted_at, left(text, 20) from message order by id desc limit 10;"
```

Expected: every message above is present. `/help` has `extractable = false`; the
others `true`. `extracted_at` is null everywhere — nothing extracts in Phase 1.

- [ ] **Step 4: Commit nothing, report**

There is nothing to commit. Report which rows appeared and whether the reaction
round-trip logged both a tombstone and a restore.

---

## What Phase 1 deliberately leaves undone

- No extraction, so no facts, so the tombstone path logs zeroes. It is wired and
  tested; it has nothing to act on until Phase 2.
- No `/q`. A `/q` message is stored with `extractable = false` and otherwise
  ignored.
- No history import, and no way to change `tz_offset` or `currency` from the
  chat — they are columns with defaults until a later phase adds a command.
- No background or scheduled anything.
