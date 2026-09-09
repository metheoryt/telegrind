# Freeform Fact Ingestion — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any text message, in any phrasing, is logged to Postgres, classified and field-extracted by an LLM against a spreadsheet-defined category registry, and projected into the user's Google Sheets workbook — with the existing workbook history imported first so the workbook stays rebuildable without data loss.

**Architecture:** Postgres is the source of truth. A Telegram message becomes a `message` row (the immutable log), which extraction turns into zero or more `fact` rows (category + typed fields), which projection writes into a worksheet. Categories live in a user-editable `_categories` worksheet, so adding one is a spreadsheet edit and not a deploy. The regex parsers and the `Sheet`/`Outcome`/`Loan`/`Wish` class hierarchy are deleted, because a category is now a row of data rather than a class.

**Tech Stack:** Python 3.14, aiogram 3.22, SQLAlchemy 2.0 async (asyncpg) + Alembic, gspread-asyncio, `anthropic` 0.97 structured outputs, dateparser, pydantic-extra-types, pytest + pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-09-08-freeform-fact-ingestion-design.md` — read it before Task 1. The plan argues from it; where the plan contradicts the spec, the plan's *Deviations* section below says why.

---

## Global Constraints

Every task's requirements implicitly include this section.

**Naming — these three will bite an executor who skips this section:**

- **The SQLAlchemy log model is `LoggedMessage`, table `message`.** `Message` is `aiogram.types.Message`, already imported in `middleware.py`, `start.py`, and `handlers.py`. The spec's pseudocode writes `class Message(Base)`; do not.
- **The declarative base is `Model`**, defined in `telegrind/models.py`. The spec's snippets say `Base`; do not.
- **`Fact`'s foreign key to the log is `message_pk`, not `message_id`.** This extends the spec's own naming trap: `LoggedMessage.message_id` is the *Telegram* id, `LoggedMessage.id` is the surrogate PK. `chat_pk` and `message_pk` are always surrogate PKs; `chat_id` and `message_id` are always Telegram ids. No exceptions.

**Data contracts that must not change:**

- The prod database `telegrind_pgdata` holds live `chat` rows. `Chat` and `File` keep their tables; both existing Alembic revisions stay. `down_revision` for the one new migration is `'2700e0b3a8b6'`.
- Telegram ids need `BigInteger` — `chat_id`, `message_id`.
- The `_config` worksheet format is fixed: two rows, Russian key labels `Часовой пояс (в часах)` and `Основная валюта`, values in column B. The class may be rewritten; the sheet may not.
- `Expenses`, `Loans`, and `Wishlist` hold real history with the exact headers in `sheets.py` today. The seeded registry reproduces them character for character.
- `entrypoint.sh` runs `alembic upgrade head` before `python main.py`, so the migration ships with the deploy. No manual migration step.

**Lint and style — the most likely cause of a task's verification step failing for reasons unrelated to the task:**

- ruff runs with `ANN` (flake8-annotations). **Every function, including every test function, needs a return annotation** — `def test_x() -> None:`.
- ruff runs with `T20` (flake8-print). **No `print()` in `telegrind/` or `tests/`.** Use `logging`. A throwaway script belongs in the scratchpad, outside the repo.
- ruff runs with `N` (pep8-naming), `S` (bandit), `SIM`, `B`, `UP`, `I`. Line length 88, double quotes.
- Verify with `uv run ruff check .` and `uv run ruff format --check .` before each commit.
- `ValueInputOption` members are lowercase attributes: `ValueInputOption.user_entered` (value `"USER_ENTERED"`). The spec's prose says `USER_ENTERED`; the code says `user_entered`.

**LLM call shape — settled empirically by probe 1 on 2026-09-09, `anthropic` 0.97.0, `claude-haiku-4-5`:**

```python
resp = client.messages.create(
    model=model,
    max_tokens=2048,
    system=[{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": user_text}],
    output_config={"format": {"type": "json_schema", "schema": schema}},
)
payload = json.loads(resp.content[0].text)
```

- Not `messages.parse` — it requires a static Python type, which a user-editable registry cannot supply.
- `anyOf` inside an array's `items` with a `const` discriminator **works**. Cyrillic property names work verbatim. `{"type": "string", "format": "date-time"}` is honored.
- **The current timestamp goes in the user message, never in the cached system prefix.**
- No `thinking` parameter — `claude-haiku-4-5` is the `budget_tokens` generation.
- **Never assert a prompt-cache hit in a test.** `cache_control` silently no-ops below the model's minimum cacheable prefix; the probe measured `cache_creation_input_tokens == 0` at 1047 input tokens.

**Environment variables added this phase:** `LLM_MODEL` (default `claude-haiku-4-5`), `LLM_MODEL_ESCALATE` (default `claude-sonnet-5`, unused until Phase 2 but declared now so `.env.dist` is complete). `ANTHROPIC_API_KEY` already exists. `MARVIN_AGENT_MODEL` is removed in Task 14, not before.

**Deployment:** pushing to `main` **is** the deploy. Do not push `main` during this work — the branch is `freeform-facts`. The dev stack is compose project `telegrind-dev`; never `docker compose down -v` against prod. Never tag an image `metheoryt/telegrind-bot:*`.

**Commits:** conventional-commit subject, and end every commit message with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

---

## Deviations from the spec

Four places where this plan is deliberately not what the spec says. Each is a gap the spec did not close, found while sequencing the work.

1. **`Fact` gains a non-null `chat_pk`.** The spec scopes facts to a chat *through* `message_pk` — but imported facts have `message_pk IS NULL`, so they would belong to no chat and `/rebuild` could not find them. `Fact.chat_pk` is a non-null FK to `chat.id`; `message_pk` stays nullable.
2. **Import is a command, `/import [--dry-run]`, not an implicit startup step.** A one-shot data migration that runs itself on container start is unreviewable and re-runs on every deploy. `/rebuild`'s refusal gate already makes it impossible to do damage before the import has run.
3. **Imported rows with no usable key in column A get a synthesized one** — `import-<worksheet>-<row number>`. Hand-added rows have no Telegram `message_id` there; without synthesis they stay permanently "unaccounted" and `/rebuild` refuses forever, turning the safety gate into a dead end.
4. **`importer.py` is a new module** not listed in the spec's module layout. It is one-time code with a distinct lifetime from `projection.py`, and keeping it separate makes it deletable later.
5. **Registry validation errors are logged, not messaged to the user, in Phase 1.** The spec says the bot "reports which row and why, once per registry load". A handler cannot tell a fresh load from a cached one, so "once per load" would need extra state to express faithfully; a `log.warning` per error per load is what this phase ships, and Task 15 step 15 verifies it. Surfacing it in chat belongs with `/reload`'s output in Phase 2.

---

## File structure

| file | responsibility | task |
|---|---|---|
| `telegrind/registry.py` | *new* — `Column`, `Category`, `Registry`; parse and validate `_categories`; seed it; TTL cache | 2, 4 |
| `telegrind/sheets.py` | keep `Config`; rewrite `ConfigSheet` robustly; add the `Worksheet` client; delete `Sheet`/`Transaction`/`Outcome`/`Loan`/`Wish` | 3, 14 |
| `telegrind/coerce.py` | *new* — one function per field type, plus the date fallback chain | 5 |
| `telegrind/llm.py` | *new* — JSON Schema builder, system prompt, `PROMPT_VERSION`, the extraction call | 6 |
| `telegrind/models.py` | add `LoggedMessage` and `Fact` | 7 |
| `alembic/versions/*_message_and_fact.py` | *new* — one additive migration | 7 |
| `telegrind/store.py` | *new* — message and fact repository functions | 8 |
| `telegrind/projection.py` | *new* — key format, row building, the edit diff, then the sheet writes | 9, 10 |
| `telegrind/importer.py` | *new* — one-time history import | 11 |
| `telegrind/bot/handlers/commands.py` | *new* — `/import`, `/rebuild`, `/reload` | 12 |
| `telegrind/bot/middleware.py` | inject `ags`, `registry`, `config` | 12 |
| `telegrind/bot/handlers/handlers.py` | rewrite to the ingest handlers | 13 |
| `telegrind/services/expense.py` | **delete** | 13 |
| `tests/` | *new* — unit tests for every pure function above | 1–12 |
| `tests/fixtures/extraction.yaml` | *new* — on-demand extraction eval | 14 |

---

## Task 1: Dependencies, test harness, and the config contract

There are no tests in this repo and no test runner. Nothing later in this plan can be verified until this exists, so it is first. `anthropic` is currently only a transitive dependency of `marvin`; it becomes direct here, while `marvin` stays until Task 14 removes the last import of it.

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.dist`
- Create: `tests/__init__.py`
- Create: `tests/test_harness.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a working `uv run pytest`, `anthropic>=0.97.0` as a direct dependency, and `LLM_MODEL` / `LLM_MODEL_ESCALATE` in `.env.dist`.

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` as an empty file, and `tests/test_harness.py`:

```python
import anthropic


def test_anthropic_is_a_direct_dependency() -> None:
    major, minor, *_ = (int(p) for p in anthropic.__version__.split("."))
    assert (major, minor) >= (0, 97)


async def test_asyncio_mode_is_auto() -> None:
    """An unmarked async test only runs if asyncio_mode = "auto"."""
    assert True
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_harness.py -v`
Expected: FAIL — `pytest` is not installed, so `uv run pytest` errors with a command-not-found or `No module named pytest`.

- [ ] **Step 3: Add the dependencies and pytest configuration**

In `pyproject.toml`, add one line to `[project] dependencies`. The existing list is not sorted, so put it after `"alembic>=1.13.0",`:

```toml
    "anthropic>=0.97.0",
```

Add to `[dependency-groups] dev`:

```toml
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
```

Append a new section at the end of `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Update `.env.dist`**

Replace the `# --- LLM (marvin) ---` block with:

```
# --- LLM ---
# Model used for normal extraction.
LLM_MODEL=claude-haiku-4-5
# Stronger model used by `??` re-extraction and `/reparse --model` (Phase 2).
LLM_MODEL_ESCALATE=claude-sonnet-5
ANTHROPIC_API_KEY=
```

`MARVIN_AGENT_MODEL` is gone from `.env.dist` here, but `marvin` still reads it from a real `.env` until Task 14. That is fine: `.env.dist` is the contract for a *fresh* box, and a fresh box will not run the old code.

- [ ] **Step 5: Sync and run the tests**

Run: `uv sync && uv run pytest tests/test_harness.py -v`
Expected: 2 passed. If `test_asyncio_mode_is_auto` is skipped or errors with "async def functions are not natively supported", the `asyncio_mode` setting did not land.

- [ ] **Step 6: Lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean. If `format --check` fails on files you did not touch, run `uv run ruff format tests/ pyproject.toml` and re-check — do not reformat the whole repo in this task.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .env.dist tests/
git commit -m "$(cat <<'EOF'
chore: add anthropic, pytest, and the LLM_MODEL config contract

anthropic was only a marvin transitive; it becomes direct because extraction
calls it straight. marvin stays until the regex path is deleted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Registry parsing and validation

The `_categories` worksheet is user-editable, which means every cell in it is untrusted input. A malformed row must cost you the row, never the message. This is the single largest test surface in the phase and it is pure — no Sheets, no LLM, no database.

**Files:**
- Create: `telegrind/registry.py`
- Create: `tests/test_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FIELD_TYPES: frozenset[str]` — `{"text", "number", "money", "currency", "date", "due"}`
  - `ROLLUP_TEMPLATES: frozenset[str]` — `{"balance", "sum_by_period"}`
  - `REGISTRY_HEADERS: list[str]` — the `_categories` header row
  - `Column(header: str, type: str)` — frozen dataclass
  - `Category(name, worksheet, when_to_use, columns: tuple[Column, ...], rollup: str | None)` — frozen dataclass, with `headers` property returning `["#", *column headers]`
  - `Registry(categories: tuple[Category, ...], errors: tuple[str, ...])` — frozen dataclass with `by_name(name) -> Category | None` and `by_worksheet(name) -> Category | None`
  - `FACTS_FALLBACK: Category` — the always-present `facts` category
  - `SEED_CATEGORIES: tuple[Category, ...]` — the five seeded rows
  - `parse_columns(cell: str) -> tuple[tuple[Column, ...], tuple[str, ...]]` — columns, errors
  - `parse_registry(rows: list[list[str]]) -> Registry`
  - `to_rows(categories) -> list[list[str]]` — the inverse, used by seeding in Task 4

- [ ] **Step 1: Write the failing tests**

Create `tests/test_registry.py`:

```python
from telegrind.registry import (
    FACTS_FALLBACK,
    REGISTRY_HEADERS,
    SEED_CATEGORIES,
    Column,
    parse_columns,
    parse_registry,
    to_rows,
)


def rows(*body: list[str]) -> list[list[str]]:
    return [REGISTRY_HEADERS, *body]


def test_parses_a_single_category() -> None:
    reg = parse_registry(
        rows(["expense", "Expenses", "потрачено", "Сумма:money, Комментарий", ""])
    )
    assert reg.errors == ()
    cat = reg.by_name("expense")
    assert cat is not None
    assert cat.worksheet == "Expenses"
    assert cat.when_to_use == "потрачено"
    assert cat.columns == (Column("Сумма", "money"), Column("Комментарий", "text"))
    assert cat.rollup is None


def test_type_defaults_to_text_when_omitted() -> None:
    cols, errors = parse_columns("Желание, Добавлено:date")
    assert errors == ()
    assert cols == (Column("Желание", "text"), Column("Добавлено", "date"))


def test_headers_prefix_the_key_column() -> None:
    cat = parse_registry(
        rows(["wish", "Wishlist", "чего хочется", "Желание, Добавлено:date", ""])
    ).by_name("wish")
    assert cat is not None
    assert cat.headers == ["#", "Желание", "Добавлено"]


def test_unknown_type_drops_the_row_with_an_error() -> None:
    reg = parse_registry(rows(["weird", "Weird", "?", "Значение:quantum", ""]))
    assert reg.by_name("weird") is None
    assert any("quantum" in e for e in reg.errors)


def test_missing_name_drops_the_row() -> None:
    reg = parse_registry(rows(["", "Nameless", "?", "Текст", ""]))
    assert len(reg.categories) == 1  # only the injected facts fallback
    assert any("name" in e.lower() for e in reg.errors)


def test_missing_worksheet_drops_the_row() -> None:
    reg = parse_registry(rows(["orphan", "", "?", "Текст", ""]))
    assert reg.by_name("orphan") is None
    assert any("worksheet" in e.lower() for e in reg.errors)


def test_no_columns_drops_the_row() -> None:
    reg = parse_registry(rows(["empty", "Empty", "?", "   ", ""]))
    assert reg.by_name("empty") is None
    assert any("column" in e.lower() for e in reg.errors)


def test_duplicate_name_keeps_the_first_and_reports_the_second() -> None:
    reg = parse_registry(
        rows(
            ["expense", "Expenses", "a", "Сумма:money", ""],
            ["expense", "Other", "b", "Значение:number", ""],
        )
    )
    cat = reg.by_name("expense")
    assert cat is not None
    assert cat.worksheet == "Expenses"
    assert any("duplicate" in e.lower() for e in reg.errors)


def test_duplicate_worksheet_drops_the_second_row() -> None:
    reg = parse_registry(
        rows(
            ["a", "Shared", "x", "Текст", ""],
            ["b", "Shared", "y", "Текст", ""],
        )
    )
    assert reg.by_name("a") is not None
    assert reg.by_name("b") is None
    assert any("worksheet" in e.lower() for e in reg.errors)


def test_duplicate_column_header_drops_the_row() -> None:
    reg = parse_registry(rows(["dup", "Dup", "?", "Сумма:money, Сумма:number", ""]))
    assert reg.by_name("dup") is None
    assert any("Сумма" in e for e in reg.errors)


def test_key_column_header_is_reserved() -> None:
    reg = parse_registry(rows(["bad", "Bad", "?", "#:text, Сумма:money", ""]))
    assert reg.by_name("bad") is None
    assert any("#" in e for e in reg.errors)


def test_rollup_is_kept_when_it_names_real_columns() -> None:
    cat = parse_registry(
        rows(
            [
                "loan",
                "Loans",
                "долг",
                "Сумма:money, Заёмщик:text",
                "balance(Заёмщик, Сумма)",
            ]
        )
    ).by_name("loan")
    assert cat is not None
    assert cat.rollup == "balance(Заёмщик, Сумма)"


def test_unknown_rollup_function_is_an_error_and_the_rollup_is_dropped() -> None:
    reg = parse_registry(
        rows(["loan", "Loans", "долг", "Сумма:money", "teleport(Сумма)"])
    )
    cat = reg.by_name("loan")
    assert cat is not None
    assert cat.rollup is None
    assert any("teleport" in e for e in reg.errors)


def test_rollup_naming_a_missing_column_is_an_error() -> None:
    reg = parse_registry(
        rows(["loan", "Loans", "долг", "Сумма:money", "balance(Кто, Сумма)"])
    )
    cat = reg.by_name("loan")
    assert cat is not None
    assert cat.rollup is None
    assert any("Кто" in e for e in reg.errors)


def test_malformed_rollup_syntax_is_an_error_not_a_crash() -> None:
    reg = parse_registry(rows(["loan", "Loans", "долг", "Сумма:money", "balance("]))
    assert reg.by_name("loan") is not None
    assert reg.errors != ()


def test_short_rows_do_not_raise() -> None:
    reg = parse_registry([REGISTRY_HEADERS, ["expense"], [], ["a", "B"]])
    assert reg.errors != ()
    assert reg.by_name("facts") is not None


def test_facts_fallback_is_injected_when_absent() -> None:
    reg = parse_registry(rows(["expense", "Expenses", "?", "Сумма:money", ""]))
    assert reg.by_name("facts") == FACTS_FALLBACK


def test_a_user_defined_facts_row_wins_over_the_fallback() -> None:
    reg = parse_registry(rows(["facts", "Журнал", "всё", "Текст, Дата:date", ""]))
    cat = reg.by_name("facts")
    assert cat is not None
    assert cat.worksheet == "Журнал"


def test_blank_rows_are_ignored_silently() -> None:
    reg = parse_registry(rows(["", "", "", "", ""], ["  ", "", "", "", ""]))
    assert reg.errors == ()
    assert reg.categories == (FACTS_FALLBACK,)


def test_seed_reproduces_todays_worksheet_headers_exactly() -> None:
    by_name = {c.name: c for c in SEED_CATEGORIES}
    assert by_name["expense"].headers == ["#", "Сумма", "Валюта", "Дата", "Комментарий"]
    assert by_name["loan"].headers == [
        "#",
        "Сумма",
        "Валюта",
        "Заёмщик",
        "Дата",
        "Комментарий",
    ]
    assert by_name["wish"].headers == ["#", "Желание", "Добавлено", "Исполнено"]


def test_to_rows_round_trips_through_parse_registry() -> None:
    reg = parse_registry([REGISTRY_HEADERS, *to_rows(SEED_CATEGORIES)])
    assert reg.errors == ()
    assert reg.categories == SEED_CATEGORIES
```

The header assertions in `test_seed_reproduces_todays_worksheet_headers_exactly` are copied from `telegrind/sheets.py` as it stands: `Outcome.headers`, `Loan.headers`, `Wish.headers`. If the test fails, the seed is wrong, not the test — real spreadsheets have those headers.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.registry'`

- [ ] **Step 3: Write the implementation**

Create `telegrind/registry.py`:

```python
"""The category registry: a user-editable `_categories` worksheet.

Every cell here is untrusted input. A malformed row is collected as a
validation error and dropped; it must never cost the user a message.
"""

import re
from dataclasses import dataclass

FIELD_TYPES: frozenset[str] = frozenset(
    {"text", "number", "money", "currency", "date", "due"}
)
DEFAULT_FIELD_TYPE = "text"

#: Rollup templates. The names are fixed now so the registry can validate
#: against them; the formulas themselves arrive in Phase 2 (`rollups.py`).
ROLLUP_TEMPLATES: frozenset[str] = frozenset({"balance", "sum_by_period"})

#: Column A of every category worksheet holds the row key, never a field.
KEY_HEADER = "#"

REGISTRY_WORKSHEET = "_categories"
REGISTRY_HEADERS = ["name", "worksheet", "when to use", "columns", "rollup"]

_ROLLUP_RE = re.compile(r"^\s*(\w+)\s*\((.*)\)\s*$")


@dataclass(frozen=True, slots=True)
class Column:
    header: str
    type: str = DEFAULT_FIELD_TYPE


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    worksheet: str
    when_to_use: str
    columns: tuple[Column, ...]
    rollup: str | None = None

    @property
    def headers(self) -> list[str]:
        """The worksheet's header row, key column included."""
        return [KEY_HEADER, *(c.header for c in self.columns)]

    def column(self, header: str) -> Column | None:
        for c in self.columns:
            if c.header == header:
                return c
        return None


@dataclass(frozen=True, slots=True)
class Registry:
    categories: tuple[Category, ...]
    errors: tuple[str, ...] = ()

    def by_name(self, name: str) -> Category | None:
        for c in self.categories:
            if c.name == name:
                return c
        return None

    def by_worksheet(self, worksheet: str) -> Category | None:
        for c in self.categories:
            if c.worksheet == worksheet:
                return c
        return None


FACTS_FALLBACK = Category(
    name="facts",
    worksheet="Facts",
    when_to_use="всё остальное — сохранить как есть",
    columns=(Column("Текст", "text"), Column("Дата", "date")),
)

SEED_CATEGORIES: tuple[Category, ...] = (
    Category(
        name="expense",
        worksheet="Expenses",
        when_to_use="потраченная сумма",
        columns=(
            Column("Сумма", "money"),
            Column("Валюта", "currency"),
            Column("Дата", "date"),
            Column("Комментарий", "text"),
        ),
        rollup="sum_by_period(Дата, Сумма)",
    ),
    Category(
        name="loan",
        worksheet="Loans",
        when_to_use="деньги в долг или возврат долга",
        columns=(
            Column("Сумма", "money"),
            Column("Валюта", "currency"),
            Column("Заёмщик", "text"),
            Column("Дата", "date"),
            Column("Комментарий", "text"),
        ),
        rollup="balance(Заёмщик, Сумма)",
    ),
    Category(
        name="telemetry",
        worksheet="Telemetry",
        when_to_use="измерение о себе",
        columns=(
            Column("Метрика", "text"),
            Column("Значение", "number"),
            Column("Ед", "text"),
            Column("Дата", "date"),
        ),
    ),
    Category(
        name="wish",
        worksheet="Wishlist",
        when_to_use="чего хочется",
        columns=(
            Column("Желание", "text"),
            Column("Добавлено", "date"),
            Column("Исполнено", "text"),
        ),
    ),
    FACTS_FALLBACK,
)


def parse_columns(cell: str) -> tuple[tuple[Column, ...], tuple[str, ...]]:
    """Parse a `columns` cell: `"Сумма:money, Комментарий"`.

    Returns the columns and any validation errors. On any error the caller
    drops the whole row — a category with a column it cannot type is worse
    than no category.
    """
    columns: list[Column] = []
    errors: list[str] = []
    seen: set[str] = set()

    for spec in cell.split(","):
        spec = spec.strip()
        if not spec:
            continue
        header, _, type_name = spec.partition(":")
        header = header.strip()
        type_name = type_name.strip().lower() or DEFAULT_FIELD_TYPE
        if not header:
            errors.append(f"column spec {spec!r} has no header")
            continue
        if header == KEY_HEADER:
            errors.append(f"{KEY_HEADER!r} is the reserved key column header")
            continue
        if header in seen:
            errors.append(f"duplicate column header {header!r}")
            continue
        if type_name not in FIELD_TYPES:
            errors.append(
                f"column {header!r} has unknown type {type_name!r}; "
                f"known types are {sorted(FIELD_TYPES)}"
            )
            continue
        seen.add(header)
        columns.append(Column(header, type_name))

    if not columns and not errors:
        errors.append("no columns declared")
    return tuple(columns), tuple(errors)


def _parse_rollup(cell: str, columns: tuple[Column, ...]) -> tuple[str | None, list[str]]:
    """Validate a `rollup` cell. A bad rollup drops the rollup, not the row."""
    cell = cell.strip()
    if not cell:
        return None, []

    match = _ROLLUP_RE.match(cell)
    if not match:
        return None, [f"rollup {cell!r} is not of the form name(col, col)"]

    func, args_src = match.group(1), match.group(2)
    if func not in ROLLUP_TEMPLATES:
        return None, [
            f"unknown rollup {func!r}; known rollups are {sorted(ROLLUP_TEMPLATES)}"
        ]

    headers = {c.header for c in columns}
    args = [a.strip() for a in args_src.split(",") if a.strip()]
    if not args:
        return None, [f"rollup {cell!r} names no columns"]
    missing = [a for a in args if a not in headers]
    if missing:
        return None, [f"rollup {cell!r} names columns that do not exist: {missing}"]
    return cell, []


def _cell(row: list[str], index: int) -> str:
    """Read a cell from a possibly-short row. Sheets truncates trailing blanks."""
    return row[index].strip() if index < len(row) and row[index] else ""


def parse_registry(rows: list[list[str]]) -> Registry:
    """Parse the `_categories` worksheet values, header row included."""
    categories: list[Category] = []
    errors: list[str] = []
    names: set[str] = set()
    worksheets: set[str] = set()

    for number, row in enumerate(rows[1:], start=2):
        name = _cell(row, 0)
        worksheet = _cell(row, 1)
        when_to_use = _cell(row, 2)
        columns_cell = _cell(row, 3)
        rollup_cell = _cell(row, 4)

        if not any((name, worksheet, when_to_use, columns_cell, rollup_cell)):
            continue  # a blank row is not an error

        where = f"row {number}"
        if not name:
            errors.append(f"{where}: no category name")
            continue
        if not worksheet:
            errors.append(f"{where} ({name}): no worksheet")
            continue
        if name in names:
            errors.append(f"{where}: duplicate category name {name!r}")
            continue
        if worksheet in worksheets:
            errors.append(f"{where} ({name}): worksheet {worksheet!r} already used")
            continue

        columns, column_errors = parse_columns(columns_cell)
        if column_errors:
            errors.extend(f"{where} ({name}): {e}" for e in column_errors)
            continue

        rollup, rollup_errors = _parse_rollup(rollup_cell, columns)
        errors.extend(f"{where} ({name}): {e}" for e in rollup_errors)

        names.add(name)
        worksheets.add(worksheet)
        categories.append(
            Category(
                name=name,
                worksheet=worksheet,
                when_to_use=when_to_use,
                columns=columns,
                rollup=rollup,
            )
        )

    if FACTS_FALLBACK.name not in names:
        categories.append(FACTS_FALLBACK)

    return Registry(tuple(categories), tuple(errors))


def to_rows(categories: tuple[Category, ...]) -> list[list[str]]:
    """Render categories back to `_categories` rows, for seeding."""
    return [
        [
            c.name,
            c.worksheet,
            c.when_to_use,
            ", ".join(
                col.header
                if col.type == DEFAULT_FIELD_TYPE
                else f"{col.header}:{col.type}"
                for col in c.columns
            ),
            c.rollup or "",
        ]
        for c in categories
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/registry.py tests/test_registry.py
uv run ruff format telegrind/registry.py tests/test_registry.py
git add telegrind/registry.py tests/test_registry.py
git commit -m "$(cat <<'EOF'
feat: parse and validate the category registry

Every cell in _categories is untrusted input, so each malformed-row case
drops the row and reports why rather than raising. Contrast
ConfigSheet.get_data, which does a bare rows[i][1] and survives only
because _config is bot-created and 2x2.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: The worksheet client

`sheets.py` becomes a client for talking to a worksheet and nothing else. This task **adds** `Worksheet` alongside the existing `Sheet`/`Transaction`/`Outcome`/`Loan`/`Wish` classes rather than replacing them — those are still imported by `handlers.py` and `services/expense.py`, and deleting them now would leave the bot broken across a dozen intermediate commits. Task 13 deletes them.

Sheets I/O itself stays untested (spec: *"Sheets I/O, Telegram I/O, and whisper stay untested"*). What gets tested is the pure parsing that used to be buried inside I/O methods — including the bare `rows[i][1]` in `ConfigSheet.get_data`, which raises `IndexError` on a `_config` sheet whose column B is blank.

**Files:**
- Modify: `telegrind/sheets.py`
- Create: `tests/test_sheets.py`

**Interfaces:**
- Consumes: `telegrind.registry.KEY_HEADER` (Task 2).
- Produces:
  - `parse_config(rows: list[list[str]]) -> Config` — pure, tolerant
  - `data_range(ncols: int, first_row: int = 2) -> str` — pure, e.g. `"A2:E"`
  - `class Worksheet` with `__init__(ags, name, headers)`, and async `agw()`, `append(rows)`, `find_key(key)`, `keys()`, `update_row(row_no, row)`, `delete_row(row_no)`, `clear_data()`, `apply_filter()`, `all_values()`
  - `Config`, `ConfigSheet` unchanged in interface

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sheets.py`:

```python
from telegrind.sheets import Config, data_range, parse_config


def test_parse_config_reads_both_keys() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "3"], ["Основная валюта", "usd"]])
    assert cfg.dt_offset == 3
    assert cfg.currency == "USD"


def test_parse_config_survives_a_blank_value_column() -> None:
    cfg = parse_config([["Часовой пояс (в часах)"], ["Основная валюта", ""]])
    assert cfg == Config()


def test_parse_config_survives_an_empty_sheet() -> None:
    assert parse_config([]) == Config()


def test_parse_config_survives_a_non_numeric_offset() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "шесть"], ["Основная валюта", "KZT"]])
    assert cfg.dt_offset == Config().dt_offset


def test_parse_config_survives_an_invalid_currency() -> None:
    cfg = parse_config([["Часовой пояс (в часах)", "6"], ["Основная валюта", "XXXXX"]])
    assert cfg.currency == Config().currency


def test_data_range_covers_the_declared_columns_only() -> None:
    assert data_range(5) == "A2:E"
    assert data_range(1) == "A2:A"


def test_data_range_past_column_z() -> None:
    assert data_range(27) == "A2:AA"
```

`data_range` is what keeps `/rebuild` from touching user-added columns: it clears and rewrites the declared range, never the whole sheet.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sheets.py -v`
Expected: FAIL — `ImportError: cannot import name 'data_range' from 'telegrind.sheets'`

- [ ] **Step 3: Add the pure helpers to `telegrind/sheets.py`**

Add these imports at the top of `telegrind/sheets.py`, alongside the existing ones:

```python
import logging

from gspread.utils import rowcol_to_a1
from pydantic import ValidationError

from telegrind.registry import KEY_HEADER

log = logging.getLogger(__name__)
```

Add after the `Config` class:

```python
def parse_config(rows: list[list[str]]) -> Config:
    """Parse the `_config` worksheet. Any unreadable cell falls back to a default.

    `_config` is small and bot-created, but it is still a spreadsheet a user
    can edit, and an IndexError here costs them a message.
    """
    values: dict[str, str] = {}
    for row in rows:
        if len(row) >= 2 and row[0]:
            values[row[0].strip()] = row[1].strip()

    data: dict[str, object] = {}
    for label, field, converter in ConfigSheet.keys:
        raw = values.get(label)
        if not raw:
            continue
        try:
            data[field] = converter(raw)
        except (ValueError, TypeError):
            log.warning("_config: cannot read %s from %r, using default", field, raw)

    try:
        return Config(**data)
    except ValidationError:
        log.warning("_config: %r failed validation, using all defaults", data)
        return Config()


def data_range(ncols: int, first_row: int = 2) -> str:
    """`"A2:E"` — the declared column range, unbounded downward.

    Used by /rebuild so that clearing a category's data never reaches a
    column the user added themselves.
    """
    last = rowcol_to_a1(1, ncols).rstrip("1")
    return f"A{first_row}:{last}"
```

`parse_config` references `ConfigSheet.keys`, so it must be defined *after* `ConfigSheet`. Move it below the `ConfigSheet` class, or hoist `ConfigSheet.keys` to a module-level `CONFIG_KEYS` constant and have `ConfigSheet` reference that. Prefer the hoist — it removes the ordering hazard:

```python
CONFIG_KEYS: list[tuple[str, str, object]] = [
    ("Часовой пояс (в часах)", "dt_offset", int),
    ("Основная валюта", "currency", lambda x: x.strip().upper()),
]
```

and inside `ConfigSheet`, replace the `keys = [...]` literal with `keys = CONFIG_KEYS`. Then `parse_config` iterates `CONFIG_KEYS` and can sit anywhere.

- [ ] **Step 4: Rewrite `ConfigSheet.get_data` to use it**

Replace the body of `ConfigSheet.get_data`:

```python
    async def get_data(self) -> Config:
        if not self._cfg:
            agw, _ = await self.get_agw()
            self._cfg = parse_config(await agw.get_values())
        return self._cfg
```

- [ ] **Step 5: Add the `Worksheet` client**

Append to `telegrind/sheets.py`:

```python
class Worksheet:
    """A single worksheet, addressed by name, with a key column in A.

    Replaces the `Sheet` -> `Transaction` -> `Outcome`/`Loan`/`Wish`
    hierarchy: a category is a row of registry data now, so there is
    nothing left for a subclass to express.
    """

    def __init__(
        self, ags: AsyncioGspreadSpreadsheet, name: str, headers: list[str]
    ) -> None:
        self.ags = ags
        self.name = name
        self.headers = headers
        self._agw: AsyncioGspreadWorksheet | None = None

    async def agw(self) -> AsyncioGspreadWorksheet:
        """Get or lazily create the worksheet, writing headers on creation."""
        if self._agw is not None:
            return self._agw
        try:
            self._agw = await self.ags.worksheet(self.name)
        except WorksheetNotFound:
            self._agw = await self.ags.add_worksheet(
                self.name, rows=1, cols=len(self.headers)
            )
            await self._agw.append_row(self.headers, table_range="A1")
            await self.apply_filter()
        return self._agw

    async def all_values(self) -> list[list[str]]:
        agw = await self.agw()
        return await agw.get_values()

    async def append(self, rows: list[list[object]]) -> None:
        if not rows:
            return
        agw = await self.agw()
        await agw.append_rows(
            rows,
            value_input_option=ValueInputOption.user_entered,
            table_range="A1",
        )

    async def keys(self) -> dict[str, int]:
        """Map every key in column A to its 1-based row number."""
        agw = await self.agw()
        column = await agw.col_values(1)
        return {
            value: number
            for number, value in enumerate(column, start=1)
            if value and value != KEY_HEADER
        }

    async def find_key(self, key: str) -> int | None:
        agw = await self.agw()
        cell = await agw.find(str(key), in_column=1)
        return cell.row if cell else None

    async def update_row(self, row_no: int, row: list[object]) -> None:
        agw = await self.agw()
        await agw.update(
            [row],
            range_name=f"A{row_no}",
            value_input_option=ValueInputOption.user_entered,
        )

    async def delete_row(self, row_no: int) -> None:
        agw = await self.agw()
        await agw.delete_rows(row_no)

    async def clear_data(self) -> None:
        """Clear the declared column range below the header row.

        Never `clear()`: user-added columns outside the declared range are
        theirs, and the projection is one-way by design.
        """
        agw = await self.agw()
        await agw.batch_clear([data_range(len(self.headers))])

    async def apply_filter(self) -> None:
        # "A:A" specifically, so a user-added column is never captured.
        agw = await self.agw()
        await agw.set_basic_filter("A:A")
```

Note the difference from `Transaction`: `agw()` returns just the worksheet, not `(worksheet, created)`. Header writing and the basic filter are handled inside, so no caller has to remember to do it.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: all pass. Also run `uv run python -c "import telegrind.sheets"` to confirm nothing in the still-live regex path broke on the `CONFIG_KEYS` hoist.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check telegrind/sheets.py tests/test_sheets.py
uv run ruff format telegrind/sheets.py tests/test_sheets.py
git add telegrind/sheets.py tests/test_sheets.py
git commit -m "$(cat <<'EOF'
feat: add a worksheet client and make _config parsing tolerant

Worksheet lands alongside the Sheet hierarchy rather than replacing it —
handlers.py and services/expense.py still import Outcome/Loan/Wish, and
deleting them now would leave the bot broken for a dozen commits.

ConfigSheet.get_data did a bare rows[i][1]; a blank column B in _config
raised IndexError and cost the user a message.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Registry loading, seeding, and the TTL cache

Reading `_categories` on every message would cost one Sheets round trip per message, on top of `_config`. A 60-second in-process cache keyed by `sheet_url` fixes that; `/reload` drops it.

**Files:**
- Modify: `telegrind/registry.py`
- Modify: `tests/test_registry.py`

**Interfaces:**
- Consumes: `telegrind.sheets.Worksheet` (Task 3), `parse_registry`/`to_rows`/`SEED_CATEGORIES` (Task 2).
- Produces:
  - `async def load_registry(ags, sheet_url: str, *, now: float | None = None) -> Registry` — cached
  - `def invalidate(sheet_url: str | None = None) -> None` — `None` clears everything
  - `CACHE_TTL_SECONDS: float = 60.0`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
import pytest

import telegrind.registry as registry_module
from telegrind.registry import CACHE_TTL_SECONDS, invalidate, load_registry


class FakeWorksheet:
    """Stands in for telegrind.sheets.Worksheet. Counts round trips."""

    def __init__(self, values: list[list[str]]) -> None:
        self.values = values
        self.reads = 0
        self.appended: list[list[str]] = []

    async def all_values(self) -> list[list[str]]:
        self.reads += 1
        return self.values

    async def append(self, rows: list[list[object]]) -> None:
        self.appended.extend(rows)
        self.values = self.values + [[str(c) for c in r] for r in rows]


async def test_load_registry_parses_the_worksheet(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["expense", "Expenses", "?", "Сумма:money", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    reg = await load_registry(object(), "url-a", now=0.0)
    assert reg.by_name("expense") is not None
    assert ws.reads == 1


async def test_load_registry_caches_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["expense", "Expenses", "?", "Сумма:money", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-b", now=100.0)
    await load_registry(object(), "url-b", now=100.0 + CACHE_TTL_SECONDS - 1)
    assert ws.reads == 1


async def test_load_registry_refreshes_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["expense", "Expenses", "?", "Сумма:money", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-c", now=100.0)
    await load_registry(object(), "url-c", now=100.0 + CACHE_TTL_SECONDS + 1)
    assert ws.reads == 2


async def test_the_cache_is_keyed_by_sheet_url(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["expense", "Expenses", "?", "Сумма:money", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-d", now=0.0)
    await load_registry(object(), "url-e", now=0.0)
    assert ws.reads == 2


async def test_invalidate_drops_one_url(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["expense", "Expenses", "?", "Сумма:money", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-f", now=0.0)
    invalidate("url-f")
    await load_registry(object(), "url-f", now=0.0)
    assert ws.reads == 2


async def test_an_empty_registry_worksheet_is_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    reg = await load_registry(object(), "url-g", now=0.0)
    assert ws.appended == to_rows(SEED_CATEGORIES)
    assert reg.categories == SEED_CATEGORIES


async def test_a_header_only_registry_worksheet_is_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    await load_registry(object(), "url-h", now=0.0)
    assert ws.appended == to_rows(SEED_CATEGORIES)


async def test_a_populated_registry_worksheet_is_never_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidate()
    ws = FakeWorksheet([REGISTRY_HEADERS, ["only", "Only", "?", "Текст", ""]])
    monkeypatch.setattr(registry_module, "_worksheet", lambda ags, headers: ws)
    reg = await load_registry(object(), "url-i", now=0.0)
    assert ws.appended == []
    assert reg.by_name("only") is not None
```

The `_worksheet` seam exists so the cache and seeding logic are testable without Sheets. It is a one-line factory, and monkeypatching it is the whole reason it is a module-level function rather than an inline constructor call.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_registry'`

- [ ] **Step 3: Write the implementation**

Add to the top of `telegrind/registry.py`:

```python
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gspread_asyncio import AsyncioGspreadSpreadsheet

    from telegrind.sheets import Worksheet

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0
```

The `TYPE_CHECKING` guard matters: `telegrind.sheets` imports `KEY_HEADER` from `telegrind.registry`, so a module-level import back the other way would be circular.

Append to `telegrind/registry.py`:

```python
_cache: dict[str, tuple[float, Registry]] = {}


def _worksheet(
    ags: "AsyncioGspreadSpreadsheet", headers: list[str]
) -> "Worksheet":
    """Factory seam, so load_registry is testable without Sheets."""
    from telegrind.sheets import Worksheet

    return Worksheet(ags, REGISTRY_WORKSHEET, headers)


def invalidate(sheet_url: str | None = None) -> None:
    """Drop the cached registry for one workbook, or for all of them."""
    if sheet_url is None:
        _cache.clear()
    else:
        _cache.pop(sheet_url, None)


async def load_registry(
    ags: "AsyncioGspreadSpreadsheet",
    sheet_url: str,
    *,
    now: float | None = None,
) -> Registry:
    """Read `_categories`, seeding it if empty. Cached for CACHE_TTL_SECONDS.

    `now` is injectable so the TTL is testable without sleeping.
    """
    at = time.monotonic() if now is None else now
    cached = _cache.get(sheet_url)
    if cached and at - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    ws = _worksheet(ags, REGISTRY_HEADERS)
    rows = await ws.all_values()

    if len(rows) <= 1:
        # Absent or header-only: seed the five defaults. The worksheets
        # themselves are still created lazily on first write, so a seeded
        # category you never use adds no clutter.
        await ws.append(to_rows(SEED_CATEGORIES))
        rows = await ws.all_values()

    registry = parse_registry(rows)
    for error in registry.errors:
        log.warning("_categories: %s", error)

    _cache[sheet_url] = (at, registry)
    return registry
```

`Worksheet.agw()` writes `REGISTRY_HEADERS` when it creates the sheet, so `all_values()` on a brand-new `_categories` returns the header row and `len(rows) <= 1` catches it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/registry.py tests/test_registry.py
uv run ruff format telegrind/registry.py tests/test_registry.py
git add telegrind/registry.py tests/test_registry.py
git commit -m "$(cat <<'EOF'
feat: load, seed, and cache the category registry

60s TTL keyed by sheet_url. Without it, N categories would mean N Sheets
round trips per message — today's edit loop already does three separate
_config reads because Transaction.__init__ builds a fresh ConfigSheet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Field coercion

The model returns JSON. The sheet wants values it can type-infer, and Postgres wants something stable to store. Coercion is the bridge, and it is where the `date`/`due` distinction and the currency fallback actually happen.

An important consequence of probe 1: a column can be optional, and an optional field arrives as a **missing key**, not an empty string. Every coercer must handle absence.

**Files:**
- Create: `telegrind/coerce.py`
- Create: `tests/test_coerce.py`

**Interfaces:**
- Consumes: `telegrind.registry.Category`/`Column` (Task 2), `telegrind.sheets.Config` (Task 3).
- Produces:
  - `SHEET_DATETIME_FORMAT = "%d.%m.%y %H:%M"`
  - `def coerce_number(raw: object) -> float | str`
  - `def coerce_currency(raw: object, cfg: Config) -> str`
  - `def coerce_datetime(raw: object, cfg: Config, fallback: datetime, *, prefer_future: bool = False) -> str`
  - `def coerce_fields(cat: Category, raw: dict[str, object], cfg: Config, fallback: datetime) -> dict[str, object]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coerce.py`:

```python
from datetime import datetime, timedelta, timezone

from telegrind.coerce import (
    SHEET_DATETIME_FORMAT,
    coerce_currency,
    coerce_datetime,
    coerce_fields,
    coerce_number,
)
from telegrind.registry import Category, Column
from telegrind.sheets import Config

CFG = Config(dt_offset=6, currency="KZT")
FALLBACK = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))


def test_number_passes_a_float_through() -> None:
    assert coerce_number(82.4) == 82.4


def test_number_accepts_an_int() -> None:
    assert coerce_number(4500) == 4500.0


def test_number_normalizes_a_decimal_comma() -> None:
    assert coerce_number("82,4") == 82.4


def test_number_strips_a_currency_glyph_and_spaces() -> None:
    assert coerce_number("4 500 ₸") == 4500.0


def test_number_keeps_a_negative_sign() -> None:
    assert coerce_number("-100") == -100.0


def test_number_returns_empty_string_for_junk() -> None:
    assert coerce_number("не число") == ""


def test_number_returns_empty_string_for_none() -> None:
    assert coerce_number(None) == ""


def test_currency_upper_cases_a_valid_code() -> None:
    assert coerce_currency("usd", CFG) == "USD"


def test_currency_falls_back_to_config_when_absent() -> None:
    assert coerce_currency(None, CFG) == "KZT"


def test_currency_falls_back_to_config_when_invalid() -> None:
    assert coerce_currency("рублей", CFG) == "KZT"


def test_datetime_parses_iso_with_an_offset() -> None:
    out = coerce_datetime("2026-09-09T21:40:00+06:00", CFG, FALLBACK)
    assert out == FALLBACK.strftime(SHEET_DATETIME_FORMAT)


def test_datetime_localizes_an_iso_value_in_another_zone() -> None:
    out = coerce_datetime("2026-09-09T15:40:00+00:00", CFG, FALLBACK)
    assert out == "09.09.26 21:40"


def test_datetime_assumes_the_chat_zone_for_a_naive_iso_value() -> None:
    assert coerce_datetime("2026-09-09T21:40:00", CFG, FALLBACK) == "09.09.26 21:40"


def test_datetime_falls_back_to_dateparser() -> None:
    out = coerce_datetime("9 сентября 2026 21:40", CFG, FALLBACK)
    assert out == "09.09.26 21:40"


def test_datetime_falls_back_to_the_message_timestamp() -> None:
    assert coerce_datetime(None, CFG, FALLBACK) == "09.09.26 21:40"


def test_datetime_falls_back_to_the_message_timestamp_on_junk() -> None:
    assert coerce_datetime("когда-нибудь", CFG, FALLBACK) == "09.09.26 21:40"


def test_date_leans_past_and_due_leans_future() -> None:
    past = coerce_datetime("вторник", CFG, FALLBACK, prefer_future=False)
    future = coerce_datetime("вторник", CFG, FALLBACK, prefer_future=True)
    parsed_past = datetime.strptime(past, SHEET_DATETIME_FORMAT)
    parsed_future = datetime.strptime(future, SHEET_DATETIME_FORMAT)
    naive_fallback = FALLBACK.replace(tzinfo=None)
    assert parsed_past <= naive_fallback
    assert parsed_future >= naive_fallback


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


def test_coerce_fields_keys_by_header_in_column_order() -> None:
    out = coerce_fields(
        EXPENSE,
        {"Сумма": "4500", "Валюта": "kzt", "Дата": None, "Комментарий": " такси "},
        CFG,
        FALLBACK,
    )
    assert list(out) == ["Сумма", "Валюта", "Дата", "Комментарий"]
    assert out["Сумма"] == 4500.0
    assert out["Валюта"] == "KZT"
    assert out["Дата"] == "09.09.26 21:40"
    assert out["Комментарий"] == "такси"


def test_coerce_fields_fills_a_missing_key() -> None:
    out = coerce_fields(EXPENSE, {"Сумма": 4500}, CFG, FALLBACK)
    assert out["Комментарий"] == ""
    assert out["Валюта"] == "KZT"
    assert out["Дата"] == "09.09.26 21:40"


def test_coerce_fields_ignores_a_field_the_registry_does_not_declare() -> None:
    out = coerce_fields(EXPENSE, {"Сумма": 1, "Придумано": "x"}, CFG, FALLBACK)
    assert "Придумано" not in out


def test_coerce_fields_never_writes_the_key_column() -> None:
    out = coerce_fields(EXPENSE, {"#": "9999.1", "Сумма": 1}, CFG, FALLBACK)
    assert "#" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_coerce.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.coerce'`

- [ ] **Step 3: Write the implementation**

Create `telegrind/coerce.py`:

```python
"""Turn model output into values a worksheet and Postgres both accept.

The registry's column type decides which coercer runs. Every coercer must
tolerate a *missing* value, not just an empty one: structured outputs allows
a partial `required` list, so an optional column arrives as an absent key.
"""

import logging
import re
from datetime import datetime

import dateparser
from pydantic import TypeAdapter, ValidationError
from pydantic_extra_types.currency_code import Currency

from telegrind.registry import Category
from telegrind.sheets import Config

log = logging.getLogger(__name__)

SHEET_DATETIME_FORMAT = "%d.%m.%y %H:%M"

_NOT_NUMERIC = re.compile(r"[^0-9.\-]")
_CURRENCY_ADAPTER = TypeAdapter(Currency)


def coerce_number(raw: object) -> float | str:
    """A float, or `""` when there is nothing numeric to read."""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", ".")
    text = _NOT_NUMERIC.sub("", text)
    try:
        return float(text)
    except ValueError:
        log.debug("cannot read a number from %r", raw)
        return ""


def coerce_currency(raw: object, cfg: Config) -> str:
    """An ISO 4217 code, falling back to the workbook's configured currency."""
    if raw:
        code = str(raw).strip().upper()
        try:
            return str(_CURRENCY_ADAPTER.validate_python(code))
        except ValidationError:
            log.debug("%r is not an ISO 4217 code, using %s", raw, cfg.currency)
    return str(cfg.currency).upper()


def coerce_datetime(
    raw: object,
    cfg: Config,
    fallback: datetime,
    *,
    prefer_future: bool = False,
) -> str:
    """ISO first, then dateparser, then the message's own timestamp.

    `prefer_future` is what separates `due` from `date`: an ambiguous
    reference like "во вторник" resolves forward for a due column and
    backward for a date column.
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
                    "TIMEZONE": cfg.tzname,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future" if prefer_future else "past",
                    "RELATIVE_BASE": fallback.replace(tzinfo=None),
                },
            )

    if parsed is None:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=cfg.tz)

    return cfg.localized(parsed).strftime(SHEET_DATETIME_FORMAT)


def coerce_fields(
    cat: Category,
    raw: dict[str, object],
    cfg: Config,
    fallback: datetime,
) -> dict[str, object]:
    """Coerce one fact's fields, keyed by header, in the category's column order.

    Fields the registry does not declare are dropped: the model is not
    allowed to invent a column, and column A is never a field.
    """
    out: dict[str, object] = {}
    for column in cat.columns:
        value = raw.get(column.header)
        match column.type:
            case "number" | "money":
                out[column.header] = coerce_number(value)
            case "currency":
                out[column.header] = coerce_currency(value, cfg)
            case "date":
                out[column.header] = coerce_datetime(value, cfg, fallback)
            case "due":
                out[column.header] = coerce_datetime(
                    value, cfg, fallback, prefer_future=True
                )
            case _:
                out[column.header] = "" if value is None else str(value).strip()
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_coerce.py -v`
Expected: all pass. If `test_date_leans_past_and_due_leans_future` is flaky, it is because 2026-09-09 is itself a Tuesday — `PREFER_DATES_FROM` then returns the same day for both. The assertions use `<=` and `>=` for exactly that reason; do not tighten them.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/coerce.py tests/test_coerce.py
uv run ruff format telegrind/coerce.py tests/test_coerce.py
git add telegrind/coerce.py tests/test_coerce.py
git commit -m "$(cat <<'EOF'
feat: coerce model output into sheet values by registry column type

date and due differ only in PREFER_DATES_FROM, so past-vs-future is a
property of the column rather than a global rule of the prompt.

Every coercer tolerates a missing key: structured outputs allows a partial
required list, so an optional column arrives absent rather than empty.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: The JSON Schema builder and the system prompt

The schema is **data**, built from the registry at runtime. This is the whole reason marvin is going: it wants a static `target=Expense`, which a user-editable registry cannot supply. Probe 1 confirmed the exact shape works, so this task encodes that shape and nothing speculative.

**Files:**
- Create: `telegrind/llm.py`
- Create: `tests/test_llm.py`

**Interfaces:**
- Consumes: `telegrind.registry.Registry`/`Category`/`Column` (Task 2), `telegrind.sheets.Config` (Task 3).
- Produces:
  - `PROMPT_VERSION: str`
  - `def build_schema(registry: Registry) -> dict[str, object]`
  - `def build_system_prompt(registry: Registry, cfg: Config) -> str`
  - `def build_user_message(content: str, now: datetime) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm.py`:

```python
from datetime import datetime, timedelta, timezone

from telegrind.llm import (
    PROMPT_VERSION,
    build_schema,
    build_system_prompt,
    build_user_message,
)
from telegrind.registry import Category, Column, Registry
from telegrind.sheets import Config

CFG = Config(dt_offset=6, currency="KZT")

EXPENSE = Category(
    name="expense",
    worksheet="Expenses",
    when_to_use="потраченная сумма",
    columns=(
        Column("Сумма", "money"),
        Column("Валюта", "currency"),
        Column("Дата", "date"),
        Column("Комментарий", "text"),
    ),
)
TELEMETRY = Category(
    name="telemetry",
    worksheet="Telemetry",
    when_to_use="измерение о себе",
    columns=(
        Column("Метрика", "text"),
        Column("Значение", "number"),
        Column("Дата", "date"),
    ),
)
LOAN = Category(
    name="loan",
    worksheet="Loans",
    when_to_use="долг",
    columns=(Column("Сумма", "money"), Column("Срок", "due")),
)
REG = Registry((EXPENSE, TELEMETRY, LOAN))


def branches(schema: dict) -> list[dict]:
    return schema["properties"]["facts"]["items"]["anyOf"]


def test_schema_wraps_an_array_of_anyof_branches() -> None:
    schema = build_schema(REG)
    assert schema["type"] == "object"
    assert schema["required"] == ["facts"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["facts"]["type"] == "array"
    assert len(branches(schema)) == 3


def test_one_branch_per_category_discriminated_by_a_const() -> None:
    consts = [b["properties"]["category"]["const"] for b in branches(REG_SCHEMA)]
    assert consts == ["expense", "telemetry", "loan"]


def test_every_branch_closes_additional_properties() -> None:
    assert all(b["additionalProperties"] is False for b in branches(REG_SCHEMA))


def test_every_column_is_required_alongside_the_discriminator() -> None:
    expense = branches(REG_SCHEMA)[0]
    assert expense["required"] == ["category", "Сумма", "Валюта", "Дата", "Комментарий"]


def test_money_and_number_become_json_numbers() -> None:
    expense, telemetry = branches(REG_SCHEMA)[0], branches(REG_SCHEMA)[1]
    assert expense["properties"]["Сумма"]["type"] == "number"
    assert telemetry["properties"]["Значение"]["type"] == "number"


def test_text_becomes_a_json_string() -> None:
    assert branches(REG_SCHEMA)[0]["properties"]["Комментарий"] == {"type": "string"}


def test_date_and_due_both_declare_the_date_time_format() -> None:
    date_prop = branches(REG_SCHEMA)[0]["properties"]["Дата"]
    due_prop = branches(REG_SCHEMA)[2]["properties"]["Срок"]
    assert date_prop["type"] == due_prop["type"] == "string"
    assert date_prop["format"] == due_prop["format"] == "date-time"


def test_date_and_due_differ_only_in_their_description() -> None:
    date_prop = branches(REG_SCHEMA)[0]["properties"]["Дата"]
    due_prop = branches(REG_SCHEMA)[2]["properties"]["Срок"]
    assert "past" in date_prop["description"]
    assert "future" in due_prop["description"]


def test_currency_describes_iso_4217() -> None:
    prop = branches(REG_SCHEMA)[0]["properties"]["Валюта"]
    assert prop["type"] == "string"
    assert "4217" in prop["description"]


def test_the_key_column_is_never_in_the_schema() -> None:
    assert all("#" not in b["properties"] for b in branches(REG_SCHEMA))


def test_the_prompt_names_every_category_and_its_when_to_use() -> None:
    prompt = build_system_prompt(REG, CFG)
    for cat in REG.categories:
        assert cat.name in prompt
        assert cat.when_to_use in prompt


def test_the_prompt_states_the_loan_sign_convention() -> None:
    prompt = build_system_prompt(REG, CFG)
    assert "-100" in prompt
    assert "+100" in prompt


def test_the_prompt_names_the_default_currency() -> None:
    assert "KZT" in build_system_prompt(REG, CFG)


def test_the_prompt_carries_no_timestamp() -> None:
    """A `now` in the cached prefix invalidates the cache on every message."""
    prompt = build_system_prompt(REG, CFG)
    assert "2026" not in prompt


def test_the_user_message_carries_the_timestamp_and_the_content() -> None:
    now = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))
    msg = build_user_message("4500 такси", now)
    assert "2026-09-09T21:40:00+06:00" in msg
    assert "4500 такси" in msg


def test_prompt_version_is_set() -> None:
    assert PROMPT_VERSION


REG_SCHEMA = build_schema(REG)
```

Note `REG_SCHEMA` is defined at the bottom and used above — that works because the module body runs before any test does. If that reads badly to you, hoist it above the tests; it must stay a module-level constant so the schema is built once.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.llm'`

- [ ] **Step 3: Write the implementation**

Create `telegrind/llm.py`:

```python
"""Extraction: registry -> JSON Schema -> one Anthropic structured-outputs call.

The schema is data, built per registry at runtime. That is why marvin is
gone: `marvin.extract_async(target=Expense)` needs a compile-time type, and
a category is a spreadsheet row.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from anthropic import AsyncAnthropic

from telegrind.registry import Category, Registry
from telegrind.sheets import Config

log = logging.getLogger(__name__)

#: Bump whenever the prompt or the schema shape changes. Every fact records
#: the version that produced it, which is what makes `/reparse --stale`
#: answerable in Phase 2.
PROMPT_VERSION = "2026-09-09.1"

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2048

_DATE_DESCRIPTION = (
    "ISO 8601 date-time with a UTC offset. If the message gives no date, use "
    "the current time from the user message. An ambiguous reference resolves "
    "to the nearest matching moment in the past."
)
_DUE_DESCRIPTION = (
    "ISO 8601 date-time with a UTC offset. This is a deadline or a due date, "
    "so an ambiguous reference resolves to the nearest matching moment in the "
    "future."
)
_CURRENCY_DESCRIPTION = (
    "Three-letter uppercase ISO 4217 code, e.g. KZT, USD, EUR, THB. Translate "
    "names and symbols: тенге -> KZT, рублей -> RUB, бат -> THB, $ -> USD."
)


@dataclass(frozen=True, slots=True)
class RawFact:
    """One fact as the model returned it, before coercion."""

    category: str
    fields: dict[str, object]


def _property_schema(column_type: str) -> dict[str, object]:
    match column_type:
        case "number" | "money":
            return {"type": "number"}
        case "currency":
            return {"type": "string", "description": _CURRENCY_DESCRIPTION}
        case "date":
            return {
                "type": "string",
                "format": "date-time",
                "description": _DATE_DESCRIPTION,
            }
        case "due":
            return {
                "type": "string",
                "format": "date-time",
                "description": _DUE_DESCRIPTION,
            }
        case _:
            return {"type": "string"}


def _branch(cat: Category) -> dict[str, object]:
    properties: dict[str, object] = {
        "category": {"type": "string", "const": cat.name}
    }
    for column in cat.columns:
        properties[column.header] = _property_schema(column.type)
    return {
        "type": "object",
        "properties": properties,
        "required": ["category", *(c.header for c in cat.columns)],
        "additionalProperties": False,
    }


def build_schema(registry: Registry) -> dict[str, object]:
    """`{"facts": [ anyOf: one branch per category ]}`.

    Probe 1 (2026-09-09, claude-haiku-4-5, anthropic 0.97.0) confirmed that
    `anyOf` inside an array's `items` with a `const` discriminator is
    accepted, and that Cyrillic property names work verbatim.
    """
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {"anyOf": [_branch(c) for c in registry.categories]},
            }
        },
        "required": ["facts"],
        "additionalProperties": False,
    }


def build_system_prompt(registry: Registry, cfg: Config) -> str:
    """The cached prefix. It must never contain a timestamp."""
    lines = [
        "You extract structured facts from a personal life-log message.",
        "",
        "Categories:",
    ]
    for cat in registry.categories:
        headers = ", ".join(f"{c.header} ({c.type})" for c in cat.columns)
        lines.append(f"- {cat.name} — {cat.when_to_use}. Fields: {headers}.")
    lines += [
        "",
        "Rules:",
        "- One message may hold several facts. Return one array element each.",
        "- Return an empty array only for a message that states no fact at all.",
        "- Anything you cannot confidently place goes to the `facts` category,",
        "  with the message text kept verbatim. Never drop a fact.",
        f"- When no currency is given, use {str(cfg.currency).upper()}.",
        "- Loan amounts carry a sign convention: a loan given out is negative",
        "  (-100), a repayment received is positive (+100). A bare amount with",
        "  no direction stated means a loan given out, so -100.",
        "- Do not invent fields. Do not invent values. An unstated text field",
        "  is an empty string.",
        "- Keep the user's own wording in text fields; do not translate it.",
    ]
    return "\n".join(lines)


def build_user_message(content: str, now: datetime) -> str:
    """The uncached suffix. The timestamp lives here, deliberately.

    Prompt caching is a prefix match, so interpolating `now` into the system
    prompt — as today's EXTRACT_INSTRUCTION does — would invalidate the cache
    on every single message.
    """
    return f"Current time: {now.isoformat()}\n\nMessage:\n{content}"


def _client() -> AsyncAnthropic:
    return AsyncAnthropic()


def current_model() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODEL


async def extract(
    content: str,
    registry: Registry,
    cfg: Config,
    now: datetime,
    model: str | None = None,
) -> tuple[list[RawFact], str]:
    """One call, zero or more facts. Returns the facts and the model used."""
    model = model or current_model()
    resp = await _client().messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": build_system_prompt(registry, cfg),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": build_user_message(content, now)}],
        output_config={
            "format": {"type": "json_schema", "schema": build_schema(registry)}
        },
    )

    if resp.stop_reason == "refusal":
        log.warning("extraction refused: %s", resp.stop_details)
        return [], model
    if resp.stop_reason == "max_tokens":
        log.warning("extraction hit max_tokens; output is truncated JSON")
        return [], model

    try:
        payload = json.loads(resp.content[0].text)
    except (IndexError, AttributeError, json.JSONDecodeError):
        log.exception("extraction returned unreadable output")
        return [], model

    facts: list[RawFact] = []
    for item in payload.get("facts", []):
        category = item.get("category")
        if registry.by_name(category) is None:
            log.warning("model returned unknown category %r, dropping", category)
            continue
        facts.append(
            RawFact(
                category=category,
                fields={k: v for k, v in item.items() if k != "category"},
            )
        )
    return facts, model
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: all pass. No test in this file makes a network call — `extract` is exercised by Task 14's on-demand fixture set and by manual verification in Task 15.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/llm.py tests/test_llm.py
uv run ruff format telegrind/llm.py tests/test_llm.py
git add telegrind/llm.py tests/test_llm.py
git commit -m "$(cat <<'EOF'
feat: build the extraction schema and prompt from the registry

Schema is data, built per registry at runtime — the reason marvin goes, since
extract_async wants a compile-time target and a category is a spreadsheet row.

The timestamp is in the user message, not the system prefix: prompt caching
is a prefix match, so today's EXTRACT_INSTRUCTION interpolation of `now`
would invalidate the cache on every message.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `LoggedMessage`, `Fact`, and the migration

One additive Alembic migration. `Chat` and `File` are untouched, and both existing revisions stay — `telegrind_pgdata` holds live rows with real `sheet_url` values.

**Files:**
- Modify: `telegrind/models.py`
- Create: `alembic/versions/<generated>_message_and_fact.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `LoggedMessage` — table `message`, with `content` property
  - `Fact` — table `fact`
  - `ORIGIN_EXTRACTED = "extracted"`, `ORIGIN_IMPORTED = "imported"`
  - `KIND_TEXT = "text"`, `KIND_VOICE = "voice"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_models.py`:

```python
from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    ORIGIN_EXTRACTED,
    ORIGIN_IMPORTED,
    Chat,
    Fact,
    LoggedMessage,
)


def test_table_names() -> None:
    assert LoggedMessage.__tablename__ == "message"
    assert Fact.__tablename__ == "fact"


def test_chat_and_file_tables_are_untouched() -> None:
    assert Chat.__tablename__ == "chat"
    assert {"id", "chat_id", "sheet_url"} <= set(Chat.__table__.columns.keys())


def test_telegram_ids_are_bigints() -> None:
    assert LoggedMessage.__table__.c.message_id.type.__class__.__name__ == "BigInteger"


def test_message_is_unique_per_chat() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in LoggedMessage.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_pk", "message_id") in constraints


def test_fact_is_unique_per_message_and_seq() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Fact.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("message_pk", "seq") in constraints
    assert ("chat_pk", "sheet_key", "worksheet") in constraints


def test_fact_is_chat_scoped_even_without_a_message() -> None:
    """Imported facts have no message, so chat_pk carries the scoping."""
    assert Fact.__table__.c.chat_pk.nullable is False
    assert Fact.__table__.c.message_pk.nullable is True


def test_imported_facts_need_no_model_or_prompt_version() -> None:
    assert Fact.__table__.c.model.nullable is True
    assert Fact.__table__.c.prompt_version.nullable is True


def test_content_prefers_the_transcript() -> None:
    msg = LoggedMessage(kind=KIND_VOICE, text=None, transcript="вес 82.4")
    assert msg.content == "вес 82.4"


def test_content_falls_back_to_text() -> None:
    msg = LoggedMessage(kind=KIND_TEXT, text="4500 такси", transcript=None)
    assert msg.content == "4500 такси"


def test_content_is_empty_when_there_is_nothing() -> None:
    assert LoggedMessage(kind=KIND_TEXT).content == ""


def test_origin_constants() -> None:
    assert ORIGIN_EXTRACTED == "extracted"
    assert ORIGIN_IMPORTED == "imported"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'LoggedMessage' from 'telegrind.models'`

- [ ] **Step 3: Write the models**

Rewrite `telegrind/models.py`. Keep `Chat` and `File` exactly as they are; add the imports and the two new classes:

```python
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

KIND_TEXT = "text"
KIND_VOICE = "voice"

ORIGIN_EXTRACTED = "extracted"
ORIGIN_IMPORTED = "imported"


class Model(AsyncAttrs, DeclarativeBase):
    pass


class Chat(Model):
    __tablename__ = "chat"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_chat_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column("chat_id", BigInteger)
    sheet_url: Mapped[str | None]


class File(Model):
    __tablename__ = "file"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str]
    filename: Mapped[str]


class LoggedMessage(Model):
    """The log. Appended on first sight, overwritten on a Telegram edit.

    Named LoggedMessage, not Message: `aiogram.types.Message` is imported in
    middleware.py, start.py and handlers.py, and a shadowed name there is a
    silent bug.
    """

    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("chat_pk", "message_id", name="uq_message_chat_pk_message_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: FK to chat.id — the surrogate PK, NOT the Telegram chat_id.
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    #: Telegram's own message id.
    message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str]
    text: Mapped[str | None]
    transcript: Mapped[str | None]
    transcript_model: Mapped[str | None]
    audio_file_id: Mapped[str | None]
    audio_duration: Mapped[int | None]
    tg_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def content(self) -> str:
        """What extraction reads."""
        return self.transcript or self.text or ""


class Fact(Model):
    """A replaceable derivation of a LoggedMessage — or of an imported row.

    Facts are chat-scoped through chat_pk rather than only through the
    message, because an imported fact has no message: its source text was
    never logged.
    """

    __tablename__ = "fact"
    __table_args__ = (
        UniqueConstraint("message_pk", "seq", name="uq_fact_message_pk_seq"),
        UniqueConstraint(
            "chat_pk", "worksheet", "sheet_key", name="uq_fact_chat_pk_worksheet_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE")
    )
    #: 1-based position within the message.
    seq: Mapped[int]
    category: Mapped[str]
    #: {header: coerced value} — exactly what projection writes.
    fields: Mapped[dict] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(default=ORIGIN_EXTRACTED)
    model: Mapped[str | None]
    prompt_version: Mapped[str | None]
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    worksheet: Mapped[str]
    #: What sits in column A: "<telegram message_id>.<seq>".
    sheet_key: Mapped[str]
```

`UNIQUE (message_pk, seq)` is safe for imported facts: Postgres treats NULLs as distinct, so every imported fact's `(NULL, 1)` is its own value. `UNIQUE (chat_pk, worksheet, sheet_key)` is what makes the import idempotent and re-runnable.

- [ ] **Step 4: Generate the migration**

The dev Postgres must be up and at the current head first:

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "message and fact"
```

Then **read the generated file** and confirm three things:
- `down_revision` is `'2700e0b3a8b6'`. If autogenerate chained it elsewhere, fix it by hand.
- It contains only `op.create_table("message", ...)` and `op.create_table("fact", ...)` plus their constraints. **Any `op.drop_*` or `op.alter_column` touching `chat` or `file` is a bug** — autogenerate produced it from a model drift, not from this task. Delete those lines and find out why they appeared.
- `message.message_id` is `sa.BigInteger()`, and both `raw` and `fields` are `postgresql.JSONB`.

- [ ] **Step 5: Verify the migration round-trips**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: three clean runs. A failure on `downgrade` usually means the FK drop order is wrong — `fact` must go before `message`.

- [ ] **Step 6: Run the tests and lint**

Run: `uv run pytest -v && uv run ruff check telegrind/models.py tests/test_models.py`
Expected: all pass. `alembic/versions/` is excluded from ty and its `ANN` noise is not a concern, but ruff still lints it — if the generated file trips a rule, `uv run ruff format alembic/versions/` first.

- [ ] **Step 7: Commit**

```bash
git add telegrind/models.py alembic/versions/ tests/test_models.py
git commit -m "$(cat <<'EOF'
feat: add the message log and fact tables

One additive migration on 2700e0b3a8b6. chat and file are untouched —
telegrind_pgdata holds live rows with real sheet_urls.

Fact carries a non-null chat_pk as well as a nullable message_pk: an
imported fact has no message, so message_pk alone could not scope it to a
chat and /rebuild would not find it. The (chat_pk, worksheet, sheet_key)
unique constraint is what makes the import idempotent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: The message and fact repository

Thin functions over the session. The queries themselves are exercised by manual verification in Task 15 — the spec puts Sheets, Telegram, and whisper I/O outside the unit-test boundary, and database I/O sits with them. What *is* tested here is the pure part: which fields get lifted off an `aiogram.types.Message`, because that is where a silent data loss would hide.

**Files:**
- Create: `telegrind/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: `telegrind.models` (Task 7).
- Produces:
  - `def message_values(msg: Message) -> dict[str, object]` — pure
  - `async def upsert_message(session, chat, msg, *, kind=None) -> tuple[LoggedMessage, bool]`
  - `async def get_message(session, chat_pk: int, message_id: int) -> LoggedMessage | None`
  - `async def facts_for_message(session, message_pk: int) -> list[Fact]`
  - `async def facts_for_chat(session, chat_pk: int, worksheet: str | None = None) -> list[Fact]`
  - `async def fact_keys_for_worksheet(session, chat_pk: int, worksheet: str) -> set[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store.py`:

```python
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from telegrind.models import KIND_TEXT, KIND_VOICE
from telegrind.store import message_kind, message_values

TG_DATE = datetime(2026, 9, 9, 15, 40, tzinfo=timezone.utc)
LOCAL = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))


def text_message(**overrides: object) -> SimpleNamespace:
    base = {
        "message_id": 4821,
        "date": TG_DATE,
        "edit_date": None,
        "text": "4500 такси",
        "caption": None,
        "voice": None,
        "forward_origin": None,
    }
    base.update(overrides)
    return SimpleNamespace(
        model_dump=lambda mode=None: {"message_id": base["message_id"]}, **base
    )


def test_kind_of_a_text_message() -> None:
    assert message_kind(text_message()) == KIND_TEXT


def test_kind_of_a_voice_message() -> None:
    voice = SimpleNamespace(file_id="AwAC", duration=9)
    assert message_kind(text_message(text=None, voice=voice)) == KIND_VOICE


def test_values_lift_the_text() -> None:
    values = message_values(text_message())
    assert values["message_id"] == 4821
    assert values["kind"] == KIND_TEXT
    assert values["text"] == "4500 такси"
    assert values["tg_date"] == TG_DATE
    assert values["edited_at"] is None


def test_values_prefer_a_caption_when_there_is_no_text() -> None:
    values = message_values(text_message(text=None, caption="4500 такси"))
    assert values["text"] == "4500 такси"


def test_values_lift_the_voice_file_and_duration() -> None:
    voice = SimpleNamespace(file_id="AwACAgIAA", duration=9)
    values = message_values(text_message(text=None, voice=voice))
    assert values["kind"] == KIND_VOICE
    assert values["audio_file_id"] == "AwACAgIAA"
    assert values["audio_duration"] == 9
    assert values["text"] is None


def test_values_carry_the_edit_timestamp() -> None:
    edited = datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc)
    values = message_values(text_message(edit_date=edited))
    assert values["edited_at"] == edited


def test_values_use_the_forward_origin_date_when_present() -> None:
    origin_date = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    values = message_values(
        text_message(forward_origin=SimpleNamespace(date=origin_date))
    )
    assert values["tg_date"] == origin_date


def test_values_always_carry_a_raw_dump() -> None:
    assert message_values(text_message())["raw"] == {"message_id": 4821}


def test_values_never_carry_a_transcript_for_text() -> None:
    values = message_values(text_message())
    assert values["transcript"] is None
    assert values["transcript_model"] is None
```

`SimpleNamespace` stands in for `aiogram.types.Message` on purpose: constructing a real one needs a `Chat`, a `From`, and a valid date, and none of that is what these assertions are about.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.store'`

- [ ] **Step 3: Write the implementation**

Create `telegrind/store.py`:

```python
"""Message and fact repository.

The log is append-on-first-sight, overwrite-on-edit. Nothing here deletes a
message row: a Telegram delete removes facts, never the log.
"""

from typing import Any

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import KIND_TEXT, KIND_VOICE, Chat, Fact, LoggedMessage


def message_kind(msg: Message) -> str:
    return KIND_VOICE if getattr(msg, "voice", None) else KIND_TEXT


def message_values(msg: Message) -> dict[str, Any]:
    """Lift the log columns off an aiogram message.

    A forwarded message's own date is the date of the forwarded content,
    which is what `services/expense.py` used too — a forward of last week's
    receipt should not be dated today.
    """
    kind = message_kind(msg)
    voice = getattr(msg, "voice", None)
    origin = getattr(msg, "forward_origin", None)

    return {
        "message_id": msg.message_id,
        "kind": kind,
        "text": None if kind == KIND_VOICE else (msg.text or msg.caption),
        "transcript": None,
        "transcript_model": None,
        "audio_file_id": voice.file_id if voice else None,
        "audio_duration": voice.duration if voice else None,
        "tg_date": origin.date if origin else msg.date,
        "edited_at": getattr(msg, "edit_date", None),
        "raw": msg.model_dump(mode="json"),
    }


async def get_message(
    session: AsyncSession, chat_pk: int, message_id: int
) -> LoggedMessage | None:
    result = await session.execute(
        select(LoggedMessage).where(
            LoggedMessage.chat_pk == chat_pk,
            LoggedMessage.message_id == message_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_message(
    session: AsyncSession, chat: Chat, msg: Message
) -> tuple[LoggedMessage, bool]:
    """Append the message, or overwrite it if we have seen this id before.

    Returns `(row, created)`. Message revision history is out of scope: an
    edit overwrites the text and bumps edited_at.
    """
    values = message_values(msg)
    existing = await get_message(session, chat.id, msg.message_id)
    if existing is not None:
        # Never clobber a stored transcript with None on a text edit.
        for key, value in values.items():
            if key in ("transcript", "transcript_model") and value is None:
                continue
            setattr(existing, key, value)
        return existing, False

    row = LoggedMessage(chat_pk=chat.id, **values)
    session.add(row)
    await session.flush()
    return row, True


async def facts_for_message(session: AsyncSession, message_pk: int) -> list[Fact]:
    result = await session.execute(
        select(Fact).where(Fact.message_pk == message_pk).order_by(Fact.seq)
    )
    return list(result.scalars())


async def facts_for_chat(
    session: AsyncSession, chat_pk: int, worksheet: str | None = None
) -> list[Fact]:
    query = select(Fact).where(Fact.chat_pk == chat_pk)
    if worksheet is not None:
        query = query.where(Fact.worksheet == worksheet)
    result = await session.execute(query.order_by(Fact.id))
    return list(result.scalars())


async def fact_keys_for_worksheet(
    session: AsyncSession, chat_pk: int, worksheet: str
) -> set[str]:
    """Every sheet_key this chat's facts claim in one worksheet.

    /rebuild compares this against the worksheet's real column A to find
    rows it cannot account for.
    """
    result = await session.execute(
        select(Fact.sheet_key).where(
            Fact.chat_pk == chat_pk, Fact.worksheet == worksheet
        )
    )
    return set(result.scalars())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/store.py tests/test_store.py
uv run ruff format telegrind/store.py tests/test_store.py
git add telegrind/store.py tests/test_store.py
git commit -m "$(cat <<'EOF'
feat: message and fact repository

upsert_message never clobbers a stored transcript with None, so a text edit
to a voice message's caption cannot erase what whisper produced.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Projection — keys, rows, and the edit diff

The pure half of projection. The edit diff is the piece today's code cannot do at all: `change_row` rewrites in place, so editing `4500 такси` into `вес 82.4` has no way to *move* the row from `Expenses` to `Telemetry`.

**Files:**
- Create: `telegrind/projection.py`
- Create: `tests/test_projection.py`

**Interfaces:**
- Consumes: `telegrind.registry.Category` (Task 2), `telegrind.models.Fact` (Task 7), `telegrind.llm.RawFact` (Task 6).
- Produces:
  - `def sheet_key(message_id: int, seq: int) -> str`
  - `def fact_row(cat: Category, key: str, fields: dict[str, object]) -> list[object]`
  - `class ChangeKind(StrEnum)`: `REWRITE`, `MOVE`, `DELETE`, `APPEND`
  - `Change(kind, seq, fact: Fact | None, raw: RawFact | None)` — frozen dataclass
  - `def diff_facts(old: list[Fact], new: list[RawFact]) -> list[Change]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projection.py`:

```python
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


def test_sheet_key_is_message_id_dot_seq() -> None:
    assert sheet_key(4821, 1) == "4821.1"
    assert sheet_key(4821, 2) == "4821.2"


def test_sheet_key_is_uniform_for_single_fact_messages() -> None:
    """A message can gain a second fact on a later edit, so 1 is not special."""
    assert sheet_key(7, 1) == "7.1"


def test_fact_row_puts_the_key_in_column_a_then_the_columns_in_order() -> None:
    row = fact_row(
        EXPENSE,
        "4821.1",
        {"Сумма": 4500.0, "Валюта": "KZT", "Дата": "09.09.26 21:40", "Комментарий": "такси"},
    )
    assert row == ["4821.1", 4500.0, "KZT", "09.09.26 21:40", "такси"]


def test_fact_row_fills_a_missing_field_with_an_empty_string() -> None:
    row = fact_row(EXPENSE, "4821.1", {"Сумма": 4500.0})
    assert row == ["4821.1", 4500.0, "", "", ""]


def test_fact_row_ignores_a_field_the_category_does_not_declare() -> None:
    row = fact_row(EXPENSE, "4821.1", {"Сумма": 1.0, "Придумано": "x"})
    assert len(row) == 5


def test_diff_same_seq_same_category_rewrites_in_place() -> None:
    changes = diff_facts([stored(1, "expense")], [RawFact("expense", {"Сумма": 5000})])
    assert [c.kind for c in changes] == [ChangeKind.REWRITE]
    assert changes[0].seq == 1
    assert changes[0].fact is not None
    assert changes[0].raw is not None


def test_diff_same_seq_different_category_moves() -> None:
    changes = diff_facts([stored(1, "expense")], [RawFact("telemetry", {"Значение": 82.4})])
    assert [c.kind for c in changes] == [ChangeKind.MOVE]
    assert changes[0].fact is not None
    assert changes[0].fact.category == "expense"
    assert changes[0].raw is not None
    assert changes[0].raw.category == "telemetry"


def test_diff_seq_gone_deletes() -> None:
    changes = diff_facts([stored(1, "expense"), stored(2, "telemetry")], [RawFact("expense", {})])
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_projection.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.projection'`

- [ ] **Step 3: Write the implementation**

Create `telegrind/projection.py`:

```python
"""Facts -> worksheet rows. One-way, by design.

Row indices are never stored: delete_rows shifts them. A fact stores its
worksheet and its sheet_key, so a single-row operation is one DB read plus
one find() in one known sheet.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from telegrind.llm import RawFact
from telegrind.models import Fact
from telegrind.registry import Category

log = logging.getLogger(__name__)


def sheet_key(message_id: int, seq: int) -> str:
    """What goes in column A. Uniform, including for single-fact messages —
    a message can gain a second fact on a later edit."""
    return f"{message_id}.{seq}"


def fact_row(cat: Category, key: str, fields: dict[str, object]) -> list[object]:
    """The key, then the category's declared columns in order."""
    return [key, *(fields.get(c.header, "") for c in cat.columns)]


class ChangeKind(StrEnum):
    REWRITE = "rewrite"
    MOVE = "move"
    DELETE = "delete"
    APPEND = "append"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    seq: int
    #: The stored fact, for REWRITE / MOVE / DELETE.
    fact: Fact | None = None
    #: The newly extracted fact, for REWRITE / MOVE / APPEND.
    raw: RawFact | None = None


def diff_facts(old: list[Fact], new: list[RawFact]) -> list[Change]:
    """Diff re-extracted facts against stored ones, by seq.

    A MOVE is the case today's code cannot express: change_row rewrites in
    place, so editing "4500 такси" into "вес 82.4" needs the row deleted
    from Expenses and appended to Telemetry.

    A same-category match is always a REWRITE, never a no-op. Rewriting is
    idempotent and one API call; comparing fields to decide would be a
    second source of truth about equality.
    """
    by_seq = {f.seq: f for f in old}
    changes: list[Change] = []

    for seq, raw in enumerate(new, start=1):
        stored = by_seq.pop(seq, None)
        if stored is None:
            changes.append(Change(ChangeKind.APPEND, seq, raw=raw))
        elif stored.category == raw.category:
            changes.append(Change(ChangeKind.REWRITE, seq, fact=stored, raw=raw))
        else:
            changes.append(Change(ChangeKind.MOVE, seq, fact=stored, raw=raw))

    for seq in sorted(by_seq):
        changes.append(Change(ChangeKind.DELETE, seq, fact=by_seq[seq]))

    return sorted(changes, key=lambda c: c.seq)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_projection.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/projection.py tests/test_projection.py
uv run ruff format telegrind/projection.py tests/test_projection.py
git add telegrind/projection.py tests/test_projection.py
git commit -m "$(cat <<'EOF'
feat: sheet keys, row building, and the fact diff

The MOVE case is what today's code cannot express: change_row rewrites in
place, so editing "4500 такси" into "вес 82.4" has no way to move the row
from Expenses to Telemetry.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Projection — applying changes and rebuilding

The I/O half. `/rebuild` is the dangerous one: it clears a category's declared range and rewrites it from `fact` rows, so it must refuse rather than guess when the worksheet holds rows no fact accounts for.

**Files:**
- Modify: `telegrind/projection.py`
- Modify: `tests/test_projection.py`

**Interfaces:**
- Consumes: `telegrind.sheets.Worksheet` (Task 3), `telegrind.registry.Registry` (Task 2), `telegrind.coerce.coerce_fields` (Task 5), Task 9's pure functions.
- Produces:
  - `def unaccounted_keys(sheet_keys: set[str], fact_keys: set[str]) -> set[str]` — pure
  - `async def apply_changes(ags, session, registry, cfg, chat, msg_row, changes, *, model, prompt_version) -> list[Fact]`
  - `async def delete_facts(ags, session, registry, facts) -> int`
  - `RebuildReport(rebuilt: dict[str, int], refused: dict[str, int])` — frozen dataclass
  - `async def rebuild(ags, session, registry, chat, *, force: bool = False) -> RebuildReport`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_projection.py`:

```python
from telegrind.projection import unaccounted_keys


def test_no_unaccounted_keys_when_the_sheet_matches_the_facts() -> None:
    assert unaccounted_keys({"4821.1", "4821.2"}, {"4821.1", "4821.2"}) == set()


def test_a_sheet_row_with_no_fact_is_unaccounted() -> None:
    assert unaccounted_keys({"4821.1", "9.1"}, {"4821.1"}) == {"9.1"}


def test_a_fact_with_no_sheet_row_is_not_unaccounted() -> None:
    """A missing row is what /rebuild is for. An extra row is what it refuses over."""
    assert unaccounted_keys({"4821.1"}, {"4821.1", "4821.2"}) == set()


def test_an_untouched_workbook_is_entirely_unaccounted() -> None:
    assert unaccounted_keys({"1.1", "2.1"}, set()) == {"1.1", "2.1"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_projection.py -k unaccounted -v`
Expected: FAIL — `ImportError: cannot import name 'unaccounted_keys'`

- [ ] **Step 3: Write the implementation**

Append to `telegrind/projection.py`:

```python
from dataclasses import field as dataclass_field
from datetime import datetime

from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.coerce import coerce_fields
from telegrind.models import ORIGIN_EXTRACTED, Chat, LoggedMessage
from telegrind.registry import Registry
from telegrind.sheets import Config, Worksheet
from telegrind.store import fact_keys_for_worksheet, facts_for_chat


def _worksheet(ags: AsyncioGspreadSpreadsheet, cat: Category) -> Worksheet:
    return Worksheet(ags, cat.worksheet, cat.headers)


def unaccounted_keys(sheet_keys: set[str], fact_keys: set[str]) -> set[str]:
    """Keys present in a worksheet that no fact claims.

    A projection that can silently discard its own source is not worth the
    convenience, so /rebuild refuses when this is non-empty.
    """
    return sheet_keys - fact_keys


async def apply_changes(
    ags: AsyncioGspreadSpreadsheet,
    session: AsyncSession,
    registry: Registry,
    cfg: Config,
    chat: Chat,
    msg_row: LoggedMessage,
    changes: list[Change],
    *,
    model: str,
    prompt_version: str,
) -> list[Fact]:
    """Write one message's diff to Postgres and to the workbook.

    Postgres first, then Sheets: a fact recorded but not yet projected is
    recoverable with /rebuild, and a row written with no fact behind it is
    exactly the unaccounted state /rebuild refuses over.
    """
    written: list[Fact] = []

    for change in changes:
        if change.kind is ChangeKind.DELETE and change.fact is not None:
            await delete_facts(ags, session, registry, [change.fact])
            continue

        assert change.raw is not None
        cat = registry.by_name(change.raw.category)
        if cat is None:
            log.warning("no category %r in the registry, skipping", change.raw.category)
            continue

        key = sheet_key(msg_row.message_id, change.seq)
        fields = coerce_fields(cat, change.raw.fields, cfg, cfg.localized(msg_row.tg_date))
        row = fact_row(cat, key, fields)
        ws = _worksheet(ags, cat)

        if change.kind is ChangeKind.MOVE and change.fact is not None:
            await delete_facts(ags, session, registry, [change.fact])
            change = Change(ChangeKind.APPEND, change.seq, raw=change.raw)

        if change.kind is ChangeKind.REWRITE and change.fact is not None:
            fact = change.fact
            fact.category = cat.name
            fact.fields = fields
            fact.worksheet = cat.worksheet
            fact.sheet_key = key
            fact.model = model
            fact.prompt_version = prompt_version
            fact.extracted_at = datetime.now(tz=cfg.tz)
            row_no = await ws.find_key(key)
            if row_no is None:
                await ws.append([row])
            else:
                await ws.update_row(row_no, row)
        else:
            fact = Fact(
                chat_pk=chat.id,
                message_pk=msg_row.id,
                seq=change.seq,
                category=cat.name,
                fields=fields,
                origin=ORIGIN_EXTRACTED,
                model=model,
                prompt_version=prompt_version,
                extracted_at=datetime.now(tz=cfg.tz),
                worksheet=cat.worksheet,
                sheet_key=key,
            )
            session.add(fact)
            await ws.append([row])

        await session.flush()
        written.append(fact)

    return written


async def delete_facts(
    ags: AsyncioGspreadSpreadsheet,
    session: AsyncSession,
    registry: Registry,
    facts: list[Fact],
) -> int:
    """Delete each fact's row from its own worksheet, then the fact itself.

    The message row stays: a delete does not rewrite the log.
    """
    deleted = 0
    for fact in facts:
        cat = registry.by_worksheet(fact.worksheet)
        if cat is not None:
            row_no = await _worksheet(ags, cat).find_key(fact.sheet_key)
            if row_no is not None:
                await _worksheet(ags, cat).delete_row(row_no)
        else:
            log.warning(
                "fact %s points at worksheet %r which the registry no longer "
                "declares; dropping the fact and leaving the row",
                fact.id,
                fact.worksheet,
            )
        await session.delete(fact)
        deleted += 1
    await session.flush()
    return deleted


@dataclass(frozen=True, slots=True)
class RebuildReport:
    rebuilt: dict[str, int] = dataclass_field(default_factory=dict)
    refused: dict[str, int] = dataclass_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refused


async def rebuild(
    ags: AsyncioGspreadSpreadsheet,
    session: AsyncSession,
    registry: Registry,
    chat: Chat,
    *,
    force: bool = False,
) -> RebuildReport:
    """Re-project every category's worksheet from fact rows. No LLM cost.

    Refuses a worksheet holding rows no fact accounts for, unless forced.
    That is the guard that makes the projection safe on a workbook with
    years of pre-bot history in it.
    """
    rebuilt: dict[str, int] = {}
    refused: dict[str, int] = {}

    for cat in registry.categories:
        ws = _worksheet(ags, cat)
        values = await ws.all_values()
        if len(values) <= 1:
            sheet_keys: set[str] = set()
        else:
            sheet_keys = {
                row[0].strip()
                for row in values[1:]
                if row and row[0] and row[0].strip()
            }

        fact_keys = await fact_keys_for_worksheet(session, chat.id, cat.worksheet)
        orphans = unaccounted_keys(sheet_keys, fact_keys)
        if orphans and not force:
            refused[cat.worksheet] = len(orphans)
            continue

        facts = await facts_for_chat(session, chat.id, cat.worksheet)
        rows = [fact_row(cat, f.sheet_key, f.fields) for f in facts]
        await ws.clear_data()
        await ws.append(rows)
        await ws.apply_filter()
        rebuilt[cat.worksheet] = len(rows)

    return RebuildReport(rebuilt, refused)
```

These additions duplicate imports the file already has — `dataclasses`, `telegrind.registry`, `telegrind.models`. Merge each into the single import line at the top of the file rather than adding a second one; `uv run ruff check --fix telegrind/projection.py` does the merging for you, and `F811` flags what it cannot.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v && uv run python -c "import telegrind.projection"`
Expected: all pass, and the import is clean (this catches the circular-import risk between `projection`, `coerce`, `sheets`, and `registry`).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/projection.py tests/test_projection.py
uv run ruff format telegrind/projection.py tests/test_projection.py
git add telegrind/projection.py tests/test_projection.py
git commit -m "$(cat <<'EOF'
feat: apply fact changes and rebuild the workbook from facts

/rebuild refuses a worksheet holding rows no fact accounts for. Without that
gate the first rebuild on a real workbook writes nothing over years of
history, because the fact table starts empty.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Importing existing worksheet history

**Without this task, the first `/rebuild` destroys real data.** `Expenses`, `Loans`, and `Wishlist` hold years of rows; the `fact` table starts empty. Task 10's refusal gate makes that safe rather than silent — this task is what makes it *resolvable*.

Imported facts are re-projectable but not re-extractable: their source text was never stored. That is an honest limit, not a bug — the log starts the day the bot starts logging.

**Files:**
- Create: `telegrind/importer.py`
- Create: `tests/test_importer.py`

**Interfaces:**
- Consumes: `telegrind.registry` (Task 2), `telegrind.sheets.Worksheet` (Task 3), `telegrind.models` (Task 7), `telegrind.projection.sheet_key` (Task 9).
- Produces:
  - `def synthesized_key(worksheet: str, row_no: int) -> str`
  - `def map_row(cat, sheet_headers: list[str], row: list[str], row_no: int) -> ImportedRow`
  - `ImportedRow(sheet_key: str, fields: dict[str, object], synthesized: bool)` — frozen dataclass
  - `def plan_worksheet(cat, values: list[list[str]]) -> list[ImportedRow]` — pure
  - `async def plan_import(ags, registry) -> dict[str, list[ImportedRow]]`
  - `async def apply_import(session, chat, registry, plan) -> tuple[int, int]` — `(imported, skipped)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_importer.py`:

```python
from telegrind.importer import (
    ImportedRow,
    map_row,
    plan_worksheet,
    synthesized_key,
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
SHEET_HEADERS = ["#", "Сумма", "Валюта", "Дата", "Комментарий"]


def test_synthesized_key_is_stable_and_identifiable() -> None:
    assert synthesized_key("Expenses", 7) == "import-Expenses-7"


def test_map_row_keeps_the_telegram_key_from_column_a() -> None:
    row = map_row(EXPENSE, SHEET_HEADERS, ["4821", "4500", "KZT", "09.09.26 21:40", "такси"], 2)
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
        EXPENSE, headers, ["4821", "4500", "заметка", "USD", "09.09.26 21:40", "такси"], 2
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
    rows = plan_worksheet(EXPENSE, [SHEET_HEADERS, ["", "", "", "", ""], ["4821", "1", "KZT", "", ""]])
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_importer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.importer'`

- [ ] **Step 3: Write the implementation**

Create `telegrind/importer.py`:

```python
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
from telegrind.sheets import Worksheet
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
) -> dict[str, list[ImportedRow]]:
    """Read every declared worksheet. Reads only — writes nothing."""
    plan: dict[str, list[ImportedRow]] = {}
    for cat in registry.categories:
        ws = Worksheet(ags, cat.worksheet, cat.headers)
        rows = plan_worksheet(cat, await ws.all_values())
        if rows:
            plan[cat.name] = rows
    return plan


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
```

`Worksheet.agw()` lazily *creates* a missing worksheet, so `plan_import` will create empty `Telemetry` and `Facts` sheets as a side effect of reading them. That is acceptable — they were going to be created on the first fact anyway — but if you would rather avoid it, check `await ags.worksheets()` for the name first and skip absent ones.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_importer.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check telegrind/importer.py tests/test_importer.py
uv run ruff format telegrind/importer.py tests/test_importer.py
git add telegrind/importer.py tests/test_importer.py
git commit -m "$(cat <<'EOF'
feat: import pre-bot worksheet history as facts

Rows are read by header, not by position, so a user-added column does not
shift the fields after it. A row with no Telegram id in column A gets a
synthesized key — otherwise it stays unaccounted forever and /rebuild
refuses permanently.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---
## Task 12: Injected workbook context, and `/import` / `/rebuild` / `/reload`

Two things that belong together: the middleware starts injecting the workbook, the registry, and the config, and the first three handlers to consume them arrive. `/import` is a command rather than a startup step on purpose — pushing to `main` recreates the container, so a self-running one-shot migration would re-run on every deploy and be unreviewable besides.

**Files:**
- Modify: `telegrind/sheets.py` — add `load_config` / `invalidate_config`
- Modify: `telegrind/bot/middleware.py`
- Create: `telegrind/bot/handlers/commands.py`
- Modify: `telegrind/bot/handlers/__init__.py`
- Create: `tests/test_commands.py`

**Interfaces:**
- Consumes: `telegrind.importer` (Task 11), `telegrind.projection.rebuild`/`RebuildReport` (Task 10), `telegrind.registry.load_registry`/`invalidate` (Task 4), `telegrind.sheets.ConfigSheet` (Task 3).
- Produces:
  - `telegrind.sheets.load_config(ags, sheet_url, *, now=None) -> Config`
  - `telegrind.sheets.invalidate_config(sheet_url: str | None = None) -> None`
  - `data["ags"]`, `data["registry"]`, `data["config"]` in every handler, each `None` before onboarding finishes
  - `def format_import_plan(plan: dict[str, list[ImportedRow]]) -> str` — pure
  - `def format_rebuild_report(report: RebuildReport) -> str` — pure
  - Handlers `cmd_import`, `cmd_rebuild`, `cmd_reload`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_commands.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.bot.handlers.commands'`

- [ ] **Step 3: Add the config cache to `telegrind/sheets.py`**

`_config` deserves the same treatment as the registry: today's code builds a fresh `ConfigSheet` per `Transaction`, so the three-sheet edit loop does three separate `_config` reads for one message.

Add `import time` to the imports, then append:

```python
_config_cache: dict[str, tuple[float, Config]] = {}
CONFIG_TTL_SECONDS = 60.0


def invalidate_config(sheet_url: str | None = None) -> None:
    """Drop the cached config for one workbook, or for all of them."""
    if sheet_url is None:
        _config_cache.clear()
    else:
        _config_cache.pop(sheet_url, None)


async def load_config(
    ags: AsyncioGspreadSpreadsheet, sheet_url: str, *, now: float | None = None
) -> Config:
    """Read `_config`, cached for CONFIG_TTL_SECONDS, keyed by sheet_url."""
    at = time.monotonic() if now is None else now
    cached = _config_cache.get(sheet_url)
    if cached and at - cached[0] < CONFIG_TTL_SECONDS:
        return cached[1]

    cfg = await ConfigSheet(ags).get_data()
    _config_cache[sheet_url] = (at, cfg)
    return cfg
```

- [ ] **Step 4: Inject `ags`, `registry`, and `config` in the middleware**

Rewrite `telegrind/bot/middleware.py`:

```python
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message, Update
from gspread.exceptions import APIError, NoValidUrlKeyFound
from gspread_asyncio import AsyncioGspreadClient, AsyncioGspreadClientManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.models import Chat
from telegrind.registry import load_registry
from telegrind.sheets import load_config

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

        agcm: AsyncioGspreadClientManager = data["agcm"]
        agc: AsyncioGspreadClient = await agcm.authorize()

        # The workbook, the registry and the config are per-update, so no
        # handler opens the spreadsheet itself. All three are None before
        # onboarding finishes — and handlers gate on that *after* writing the
        # message log, never before.
        ags = None
        registry = None
        config = None
        if chat.sheet_url:
            try:
                ags = await agc.open_by_url(chat.sheet_url)
                config = await load_config(ags, chat.sheet_url)
                registry = await load_registry(ags, chat.sheet_url)
            except (APIError, NoValidUrlKeyFound):
                log.warning("cannot open %s for chat %s", chat.sheet_url, chat.chat_id)
                ags = None
                registry = None
                config = None

        data["agc"] = agc
        data["ags"] = ags
        data["chat"] = chat
        data["session"] = session
        data["registry"] = registry
        data["config"] = config

        return await handler(event, data)
```

Note the middleware's early `return None`: the original returned a bare `return`, which is the same value but trips `RET` -style consistency once the function is annotated `-> Any`. This also stops the update chain for non-`Message` updates, exactly as before.

- [ ] **Step 5: Write the commands**

Create `telegrind/bot/handlers/commands.py`:

```python
"""Maintenance commands: /import, /rebuild, /reload."""

import logging

from aiogram import flags
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.bot.router import router
from telegrind.importer import ImportedRow, apply_import, plan_import
from telegrind.models import Chat
from telegrind.projection import RebuildReport, rebuild
from telegrind.registry import Registry, invalidate
from telegrind.sheets import invalidate_config

log = logging.getLogger(__name__)

NOT_READY_TEXT = (
    "Сначала пришлите ссылку на таблицу — без неё мне некуда писать. "
    "Отправьте /start, если ссылка потерялась."
)


def format_import_plan(plan: dict[str, list[ImportedRow]]) -> str:
    if not plan:
        return "Нечего импортировать — в листах нет строк."
    lines = ["<b>Импорт истории</b>"]
    for category, rows in sorted(plan.items()):
        synthesized = sum(1 for r in rows if r.synthesized)
        suffix = f" (без ключа: {synthesized})" if synthesized else ""
        lines.append(f"{category}: {len(rows)}{suffix}")
    return "\n".join(lines)


def format_rebuild_report(report: RebuildReport) -> str:
    lines: list[str] = []
    if report.rebuilt:
        lines.append("<b>Перезаписано</b>")
        lines += [f"{ws}: {n}" for ws, n in sorted(report.rebuilt.items())]
    if report.refused:
        lines.append("<b>Отказ</b> — в этих листах есть строки без фактов:")
        lines += [f"{ws}: {n}" for ws, n in sorted(report.refused.items())]
        lines.append(
            "Сначала выполните /import, либо повторите как "
            "<code>/rebuild --force</code>, чтобы затереть эти строки."
        )
    if not lines:
        lines.append("Нечего перезаписывать — в базе нет фактов для этих листов.")
    return "\n".join(lines)


@router.message(Command("import"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def cmd_import(
    message: Message,
    command: CommandObject,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
) -> None:
    """Import pre-bot worksheet rows as facts. `--dry-run` reports only.

    Idempotent on (chat_pk, worksheet, sheet_key), so it is safe to re-run.
    """
    if ags is None or registry is None:
        await message.reply(NOT_READY_TEXT)
        return

    dry_run = "--dry-run" in (command.args or "")
    plan = await plan_import(ags, registry)
    await message.reply(format_import_plan(plan))
    if dry_run or not plan:
        return

    async with session.begin():
        imported, skipped = await apply_import(session, chat, registry, plan)
    await message.reply(f"Импортировано: {imported}, пропущено: {skipped}.")


@router.message(Command("rebuild"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def cmd_rebuild(
    message: Message,
    command: CommandObject,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
) -> None:
    """Re-project the workbook from fact rows. No LLM cost."""
    if ags is None or registry is None:
        await message.reply(NOT_READY_TEXT)
        return

    force = "--force" in (command.args or "")
    async with session.begin():
        report = await rebuild(ags, session, registry, chat, force=force)
    await message.reply(format_rebuild_report(report))


@router.message(Command("reload"))
async def cmd_reload(message: Message, chat: Chat) -> None:
    """Drop the cached `_categories` and `_config` reads."""
    invalidate(chat.sheet_url)
    invalidate_config(chat.sheet_url)
    await message.reply("Реестр категорий и настройки будут прочитаны заново.")
```

`command: CommandObject` — aiogram injects a `CommandObject`, not the `Command` filter. Annotating it `Command` type-checks but is wrong about what arrives.

- [ ] **Step 6: Register the module**

Change `telegrind/bot/handlers/__init__.py` to:

```python
from . import commands as commands, start as start, handlers as handlers
```

**Import order is registration order** and aiogram stops at the first match, so `handlers` — which ends in a catch-all `F.text` — must import last. `commands` and `start` both use explicit `Command`/`CommandStart` filters and cannot shadow each other.

- [ ] **Step 7: Run the tests and verify the dispatcher still builds**

```bash
uv run pytest -v
uv run python -c "from telegrind.bot.setup import setup_dispatcher; setup_dispatcher()"
```

Expected: all tests pass, and the dispatcher builds. The old regex handlers are still registered and still work at this point — they simply ignore the three new `data` keys.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check telegrind/ tests/
uv run ruff format telegrind/ tests/
git add telegrind/ tests/
git commit -m "$(cat <<'EOF'
feat: inject the workbook context, and add /import /rebuild /reload

The middleware now opens the spreadsheet and loads the registry and config
once per update, so no handler opens it itself. All three are None before
onboarding finishes; handlers gate on that after writing the log.

/import is a command, not a startup step: pushing to main recreates the
container, so a self-running one-shot migration would re-run on every
deploy.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: The ingest handlers, and deleting the regex path

The atomic switch-over. The handler rewrite and the deletion of the regex path are one commit because they are one change: two parsers with different opinions about the same message is worse than either alone, and deleting the old one first would leave the bot broken.

**Files:**
- Rewrite: `telegrind/bot/handlers/handlers.py`
- Modify: `telegrind/sheets.py` — delete `Sheet`, `Transaction`, `Outcome`, `Loan`, `Wish`
- Delete: `telegrind/services/expense.py`, `telegrind/services/__init__.py`
- Modify: `pyproject.toml` — drop `marvin`
- Create: `tests/test_reply.py`

**Interfaces:**
- Consumes: everything from Tasks 2–12.
- Produces:
  - `format_records(facts: list[Fact]) -> str` — pure
  - Handlers `delete_record`, `escalate_stub`, `record_voice`, `record_text`, `record_edited`

The spec lists seven handlers. Five are here; `/start` is untouched in `start.py`, the commands landed in Task 12, and two are deliberately thin in this phase: `F.voice` logs without transcribing (transcription is Phase 3) and `??` answers instead of re-extracting (escalation is Phase 2). Both exist now so a voice note is not lost and a `??` reply is not recorded as a fact.

- [ ] **Step 1: Write the failing test for the reply format**

Create `tests/test_reply.py`:

```python
from telegrind.bot.handlers.handlers import format_records
from telegrind.models import Fact


def fact(worksheet: str, key: str, fields: dict[str, object]) -> Fact:
    return Fact(
        worksheet=worksheet, sheet_key=key, fields=fields, category="x", seq=1
    )


def test_one_record_names_the_worksheet_and_the_values() -> None:
    out = format_records(
        [
            fact(
                "Expenses",
                "4821.1",
                {
                    "Сумма": 4500.0,
                    "Валюта": "KZT",
                    "Дата": "09.09.26 21:40",
                    "Комментарий": "такси",
                },
            )
        ]
    )
    assert "Expenses" in out
    assert "4500" in out
    assert "такси" in out


def test_the_pointer_is_hidden_in_a_spoiler() -> None:
    out = format_records([fact("Expenses", "4821.1", {"Сумма": 1.0})])
    assert "<tg-spoiler>4821.1@Expenses</tg-spoiler>" in out


def test_empty_fields_are_dropped_from_the_line() -> None:
    out = format_records(
        [fact("Wishlist", "9.1", {"Желание": "велосипед", "Исполнено": ""})]
    )
    assert "велосипед" in out
    assert " ·  · " not in out


def test_several_records_are_counted() -> None:
    out = format_records(
        [
            fact("Expenses", "4821.1", {"Сумма": 1.0}),
            fact("Telemetry", "4821.2", {"Значение": 82.4}),
        ]
    )
    assert "2" in out.splitlines()[0]
    assert len(out.splitlines()) == 3


def test_a_single_record_is_not_counted() -> None:
    out = format_records([fact("Expenses", "4821.1", {"Сумма": 1.0})])
    assert len(out.splitlines()) == 2


def test_no_records_says_so() -> None:
    assert format_records([]).strip()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_reply.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_records'`

- [ ] **Step 3: Rewrite `telegrind/bot/handlers/handlers.py`**

Replace the whole file:

```python
"""Freeform ingestion.

Registration order is match order: the two reply forms first, then voice,
then the catch-all text handler. `edited_message` is a separate observer and
does not compete with them.

The `sheet_url` gate moved. Every handler here writes the message log
*first* and gates on the workbook only before projection. A fact recorded
before onboarding finishes is recoverable with /rebuild; a message dropped
at the handler is gone — and "nothing you write is ever lost" is the
property this design claims.
"""

import logging

from aiogram import Bot, F, flags
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReactionTypeEmoji
from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import llm, store
from telegrind.bot.router import router
from telegrind.models import Chat, Fact
from telegrind.projection import apply_changes, delete_facts, diff_facts
from telegrind.registry import Registry
from telegrind.sheets import Config

from .start import Form

log = logging.getLogger(__name__)

LINK_MISSING_TEXT = (
    "Ссылка на вашу таблицу потерялась. Пожалуйста, отправьте её мне ещё раз."
)
NOTHING_TEXT = "Ничего не распознала, но сообщение сохранила."
MISSING_TEXT = "Отсутствует в книге..."
VOICE_PENDING_TEXT = (
    "Голосовые пока не расшифровываю, но сообщение сохранила — "
    "разберу, когда научусь."
)
ESCALATE_PENDING_TEXT = "Повторный разбор появится в следующей версии."


def format_records(facts: list[Fact]) -> str:
    """Echo what was written.

    Every write echoes: the transparency is worth the extra message, and it
    is what makes the edit and delete affordances discoverable.
    """
    if not facts:
        return NOTHING_TEXT

    lines = ["Записано:" if len(facts) == 1 else f"Записей: {len(facts)}"]
    for fact in facts:
        values = " · ".join(
            str(v) for v in fact.fields.values() if v not in (None, "")
        )
        lines.append(
            f"<b>{fact.worksheet}</b> · {values} "
            f"<tg-spoiler>{fact.sheet_key}@{fact.worksheet}</tg-spoiler>"
        )
    return "\n".join(lines)


async def _ingest(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    config: Config | None,
    state: FSMContext,
    *,
    model: str | None = None,
) -> None:
    """Log, extract, diff, project, echo."""
    async with session.begin():
        msg_row, _ = await store.upsert_message(session, chat, message)
        message_pk = msg_row.id
        content = msg_row.content
        tg_date = msg_row.tg_date

    if ags is None or registry is None or config is None:
        await state.set_state(Form.request_sheet_url)
        await message.reply(LINK_MISSING_TEXT)
        return

    if not content.strip():
        return

    facts, used_model = await llm.extract(
        content, registry, config, config.localized(tg_date), model
    )

    async with session.begin():
        msg_row = await store.get_message(session, chat.id, message.message_id)
        if msg_row is None:  # cannot happen; the upsert above flushed it
            log.error("message %s vanished between transactions", message.message_id)
            return
        old = await store.facts_for_message(session, message_pk)
        written = await apply_changes(
            ags,
            session,
            registry,
            config,
            chat,
            msg_row,
            diff_facts(old, facts),
            model=used_model,
            prompt_version=llm.PROMPT_VERSION,
        )

    await message.reply(format_records(written))


@router.message(F.reply_to_message & F.text.func(lambda t: t.strip() == "-"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def delete_record(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Delete the replied message's facts. The message row stays.

    The filter is `F.reply_to_message & (text == "-")`, not
    `F.reply_to_message.text`. Today's filter matches *every* reply and then
    falls off the end returning None — the handler matched, so aiogram stops
    propagation, and every reply that is not "-" is silently swallowed.
    """
    if ags is None or registry is None:
        await state.set_state(Form.request_sheet_url)
        await message.reply(LINK_MISSING_TEXT)
        return

    target = message.reply_to_message
    async with session.begin():
        msg_row = await store.get_message(session, chat.id, target.message_id)
        facts = (
            await store.facts_for_message(session, msg_row.id) if msg_row else []
        )
        if not facts:
            await message.reply(MISSING_TEXT)
            return
        await delete_facts(ags, session, registry, facts)

    await bot.set_message_reaction(
        chat_id=target.chat.id,
        message_id=target.message_id,
        reaction=[ReactionTypeEmoji(emoji="💩")],
    )
    await bot.set_message_reaction(
        chat_id=message.chat.id,
        message_id=message.message_id,
        reaction=[ReactionTypeEmoji(emoji="👌")],
    )


@router.message(F.reply_to_message & F.text.func(lambda t: t.strip() == "??"))
async def escalate_stub(message: Message) -> None:
    """Answer a `??` reply instead of recording it.

    Escalated re-extraction is Phase 2. Without this handler the text would
    fall through to record_text and land in the Facts sheet.
    """
    await message.reply(ESCALATE_PENDING_TEXT)


@router.message(F.voice)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_voice(message: Message, chat: Chat, session: AsyncSession) -> None:
    """Log the voice note without transcribing it.

    Transcription is Phase 3, but logging it now means /retranscribe can
    reach back over everything recorded in the meantime.
    """
    async with session.begin():
        await store.upsert_message(session, chat, message)
    await message.reply(VOICE_PENDING_TEXT)


@router.message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_text(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    config: Config | None,
    state: FSMContext,
) -> None:
    await _ingest(message, chat, session, ags, registry, config, state)


@router.edited_message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_edited(
    edited_message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    config: Config | None,
    state: FSMContext,
) -> None:
    """Re-extract and diff. A category change moves the row between sheets."""
    await _ingest(edited_message, chat, session, ags, registry, config, state)
```

- [ ] **Step 4: Delete the regex path**

```bash
git rm telegrind/services/expense.py telegrind/services/__init__.py
```

In `telegrind/sheets.py`:

- Delete the classes `Sheet`, `Transaction`, `Outcome`, `Loan`, `Wish`, and the `_amount` / `_curr` / `_date` regex fragments.
- `ConfigSheet` subclasses `Sheet` today. **Give it its own lazy get-or-create rather than building it on `Worksheet`** — `Worksheet.agw()` returns only the worksheet, and `ConfigSheet` needs the `created` flag to write its defaults exactly once:

```python
class ConfigSheet:
    ws_name = "_config"
    ws_dim = (2, 2)
    keys = CONFIG_KEYS

    def __init__(self, ags: AsyncioGspreadSpreadsheet) -> None:
        self.ags = ags
        self._agw: AsyncioGspreadWorksheet | None = None
        self._cfg: Config | None = None

    async def get_agw(self) -> tuple[AsyncioGspreadWorksheet, bool]:
        if self._agw is not None:
            return self._agw, False
        try:
            self._agw, created = await self.ags.worksheet(self.ws_name), False
        except WorksheetNotFound:
            self._agw = await self.ags.add_worksheet(
                self.ws_name, rows=self.ws_dim[0], cols=self.ws_dim[1]
            )
            created = True
        if created:
            await self.write_data(Config())
        return self._agw, created
```

`write_data` and `get_data` keep their current bodies (with `get_data` already switched to `parse_config` in Task 3). The two Russian key labels and the 2×2 shape must not change — they are already sitting in real spreadsheets.

- Remove the now-unused imports: `re`, `Pattern`, `aiogram.types.Message`, `dateparser.search.search_dates`. `Cell` is still used by `ConfigSheet.write_data`.

In `pyproject.toml`, delete the `"marvin>=3.2.2",` line.

- [ ] **Step 5: Verify nothing still references the deleted names**

```bash
uv sync
grep -rn "marvin\|ExpenseService\|Outcome\|Transaction\|class Sheet\|TIP_TEXT" telegrind/ main.py alembic/
```

Expected: the only hits are `TIP_TEXT` in `telegrind/bot/const.py` and `telegrind/bot/handlers/start.py` — onboarding still shows it. Any hit on `marvin`, `ExpenseService`, `Outcome`, `Transaction`, or `class Sheet` is a missed edit.

- [ ] **Step 6: Run the full suite, lint, and build the dispatcher**

```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run python -c "from telegrind.bot.setup import setup_dispatcher; setup_dispatcher()"
```

Expected: all tests pass, lint clean, dispatcher builds. That last command is what catches a handler whose injected kwarg name does not match what the middleware puts in `data`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat: freeform ingestion replaces the regex path

Deleting the regex parsers is not optional: two parsers with different
opinions about the same message is worse than either alone. The
Sheet -> Transaction -> Outcome/Loan/Wish hierarchy existed because
categories were compile-time; with a runtime registry a category is a row
of data, so the hierarchy has nothing left to express.

Fixes a live bug. delete_record filtered on F.reply_to_message.text and
returned None when the text was not "-" — the handler matched, so aiogram
stopped propagation and every reply that was not "-" was silently
swallowed. The filter is now F.reply_to_message & (text == "-").

The sheet_url gate moved: the message log is written first and the gate
sits before projection. A fact recorded before onboarding finishes is
recoverable with /rebuild; a message dropped at the handler is gone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---
## Task 14: The extraction fixture set

Extraction quality is not unit-testable — the model is not deterministic and the assertion would be about taste. What *is* testable is that a known message lands in the expected category. This is the seed of a proper eval set, run on demand, never in the default suite.

**Files:**
- Create: `tests/fixtures/extraction.yaml`
- Create: `tests/test_extraction_quality.py`

**Interfaces:**
- Consumes: `telegrind.llm.extract` (Task 6), `telegrind.registry.SEED_CATEGORIES` (Task 2).
- Produces: `uv run pytest -m llm` as an opt-in check.

- [ ] **Step 1: Write the fixture file**

Create `tests/fixtures/extraction.yaml`:

```yaml
# message -> the category it must land in.
# Run on demand: `uv run pytest -m llm`. Costs tokens; never in the default suite.
- message: "4500 такси"
  category: expense
- message: "5.4 usd хостинг"
  category: expense
- message: "41 бат массаж вчера вечером"
  category: expense
- message: "потратил тысячу на продукты в среду"
  category: expense
- message: "займ Вася Пупкин 500"
  category: loan
- message: "Вася вернул мне 500"
  category: loan
- message: "дал брату 20000 до конца месяца"
  category: loan
- message: "вес 82.4"
  category: telemetry
- message: "давление 120 на 80"
  category: telemetry
- message: "пробежал 5 км за 27 минут"
  category: telemetry
- message: "хочу новый монитор"
  category: wish
- message: "хочу велосипед gravel тысяч за 400"
  category: wish
- message: "позвонил маме, всё хорошо"
  category: facts
- message: "начал читать Пелевина"
  category: facts
```

- [ ] **Step 2: Write the opt-in test**

Create `tests/test_extraction_quality.py`:

```python
"""On-demand extraction eval. Costs tokens, so it is marked and deselected.

Run with: uv run pytest -m llm
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from telegrind.llm import extract
from telegrind.registry import SEED_CATEGORIES, Registry
from telegrind.sheets import Config

CASES = yaml.safe_load((Path(__file__).parent / "fixtures" / "extraction.yaml").read_text())
REG = Registry(SEED_CATEGORIES)
CFG = Config(dt_offset=6, currency="KZT")
NOW = datetime(2026, 9, 9, 21, 40, tzinfo=timezone(timedelta(hours=6)))

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set"
    ),
]


@pytest.mark.parametrize(
    ("message", "expected"),
    [(c["message"], c["category"]) for c in CASES],
    ids=[c["message"] for c in CASES],
)
async def test_message_lands_in_the_expected_category(
    message: str, expected: str
) -> None:
    facts, _ = await extract(message, REG, CFG, NOW)
    assert facts, f"nothing extracted from {message!r}"
    assert facts[0].category == expected


async def test_a_multi_fact_message_returns_both() -> None:
    facts, _ = await extract("4500 такси и вес 82.4", REG, CFG, NOW)
    assert {f.category for f in facts} == {"expense", "telemetry"}
```

- [ ] **Step 3: Register the marker and deselect it by default**

In `pyproject.toml`, extend the pytest section:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["llm: hits the Anthropic API; costs tokens; opt in with -m llm"]
addopts = "-m 'not llm'"
```

Add `pyyaml>=6.0` to `[dependency-groups] dev`.

- [ ] **Step 4: Verify both modes**

```bash
uv sync
uv run pytest -v            # extraction cases deselected
uv run pytest -m llm -v     # extraction cases only
```

Expected: the first run reports the LLM cases as deselected. The second run hits the API — a failure here is a **prompt** finding, not a code finding. Record which cases fail rather than tightening the prompt to fit them one by one; that is what makes this an eval set instead of a test.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check tests/ && uv run ruff format tests/
git add pyproject.toml uv.lock tests/
git commit -m "$(cat <<'EOF'
test: on-demand extraction eval fixtures

Extraction quality is not unit-testable, but category assignment is
checkable. Marked `llm` and deselected by default so the normal suite
stays free and offline.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Manual end-to-end verification

Handlers, Sheets I/O, and Telegram I/O are deliberately untested, so a green suite does not mean Phase 1 works. This is the task that establishes it does. Nothing here is optional, and it happens against a **scratch workbook**, not the real one.

**Files:** none — this task produces a verification record, not code.

- [ ] **Step 1: Create a scratch workbook**

Make a fresh Google Sheets document and share it as editor with `telegrind-bot@telegrind.iam.gserviceaccount.com`. Do **not** use the real workbook: `/rebuild --force` is in this checklist.

- [ ] **Step 2: Bring up the dev stack**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run python main.py
```

Expected: no startup error, and the log shows aiogram polling. Confirm you are on `freeform-facts` and have **not** pushed `main` — pushing `main` is the deploy.

- [ ] **Step 3: Onboarding still works**

Send `/start`, then the scratch workbook URL.
Expected: the intro video, then the confirmation, then the pinned message. Open the workbook: `_config` exists with the two Russian labels.

- [ ] **Step 4: The registry seeds itself**

Send `4500 такси`.
Expected: a `_categories` worksheet appears with a header row and five rows; an `Expenses` worksheet has the row `4821.1 | 4500 | KZT | <today> | такси`; the bot's reply names `Expenses` and hides `4821.1@Expenses` in a spoiler.

- [ ] **Step 5: A multi-fact message**

Send `4500 такси и вес 82.4`.
Expected: a `Telemetry` worksheet is created, the reply says `Записей: 2`, and the two keys end `.1` and `.2` with the same message id.

- [ ] **Step 6: The fallback catches everything else**

Send `позвонил маме, всё хорошо`.
Expected: a `Facts` worksheet with the text kept verbatim. **This is the property under test** — today that message would get `TIP_TEXT` and be lost.

- [ ] **Step 7: An edit rewrites in place**

Edit the `4500 такси` message to `5500 такси`.
Expected: the same row in `Expenses`, same key, amount now 5500. No second row.

- [ ] **Step 8: An edit across categories moves the row**

Edit it again to `вес 82.4`.
Expected: the row is **gone** from `Expenses` and present in `Telemetry`, with the same key. This is the case `change_row` could never do.

- [ ] **Step 9: A reply that is not `-` is no longer swallowed**

Reply to any message with `3000 обед`.
Expected: it is recorded as an expense. Before this phase, `delete_record` matched the reply and returned `None`, so the message vanished.

- [ ] **Step 10: Delete works**

Reply `-` to a recorded message.
Expected: 💩 on the target, 👌 on your reply, the worksheet row gone. Check Postgres: the `message` row is still there, its `fact` rows are not.

```bash
docker compose exec postgres psql -U postgres -c \
  "select id, message_id, kind, left(text, 30) from message order by id;"
docker compose exec postgres psql -U postgres -c \
  "select id, message_pk, seq, category, origin, worksheet, sheet_key from fact order by id;"
```

- [ ] **Step 11: A voice note is logged, not lost**

Send a voice note.
Expected: the reply says transcription is not ready yet, and `message` has a row with `kind = 'voice'` and a non-null `audio_file_id`. Phase 3's `/retranscribe` reaches back over exactly these.

- [ ] **Step 12: `/rebuild` refuses over unaccounted rows**

Type a row into `Expenses` by hand, with something arbitrary in column A. Send `/rebuild`.
Expected: a refusal naming `Expenses` and the count, and pointing at `/import`. **The worksheet is unchanged** — verify that, it is the whole point of the gate.

- [ ] **Step 13: `/import` then `/rebuild` succeeds**

Send `/import --dry-run`, then `/import`, then `/rebuild`.
Expected: the dry run reports counts and writes nothing; the import reports what it took; the rebuild then succeeds and every row survives, hand-added row included, now with a synthesized `import-Expenses-N` key.

- [ ] **Step 14: `/import` is idempotent**

Send `/import` again.
Expected: `Импортировано: 0`, everything skipped. A second import must not double the workbook.

- [ ] **Step 15: A malformed registry row costs a row, not a message**

In `_categories`, change one category's `columns` cell to `Значение:quantum`. Send `/reload`, then a message.
Expected: the message is still recorded (into `facts` if its own category was the broken one), and the log carries a `_categories: row N (...): column 'Значение' has unknown type 'quantum'` warning.

- [ ] **Step 16: A user-added column survives a rebuild**

Add a column `Мой столбец` to the right of `Expenses`' declared range, put a value in it, and send `/rebuild --force`.
Expected: your column and its value are untouched. `clear_data` clears only the declared range, and `apply_filter` stays on `"A:A"`.

- [ ] **Step 17: Record the result**

Append a short verification note to the plan file — which steps passed, and anything that surprised you — then commit:

```bash
git add docs/superpowers/plans/2026-09-09-freeform-facts-phase-1.md
git commit -m "$(cat <<'EOF'
docs: record Phase 1 manual verification

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 18: Do not deploy yet**

Phase 1 is done when this checklist passes on a scratch workbook. Deploying means merging `freeform-facts` into `main`, which recreates the prod container and runs `alembic upgrade head` against `telegrind_pgdata` — and the **first thing to do on prod is `/import`, before any `/rebuild`**. Leave that decision to Максим; do not push `main`.
