# Many sources, one log — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put an `entry` table between the Telegram message and the fact, so a
fact can come from a spreadsheet row, a receipt or a statement line as easily
as from a message — and import the v1 expense sheet through it.

**Architecture:** `entry` becomes the unit that yields facts. It owns the
verdict and the extraction state that `message` carries today, and
`fact.message_pk` becomes `fact.entry_pk`. `message` keeps only what Telegram
sent. Ingest writes both rows in one transaction; the extraction queue reads
`entry` alone; the reaction gesture gains one hop. No import machinery is
built — the v1 import is a throwaway script run by hand.

**Tech Stack:** Python 3.14, aiogram 3.27, SQLAlchemy 2 (asyncpg), Postgres 15,
alembic, Anthropic API, uv, ruff, ty, pytest (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-09-16-sources-and-entries-design.md` —
read it before Task 2. It is the authority; this plan argues from it.

## Global Constraints

- **`uv run pytest` is run as `uv run pytest`** — `addopts = "-m 'not llm'"`
  deselects `tests/test_extraction_quality.py`. That file still has to
  **collect**, so it must be updated in step with `build_prompt`'s signature.
- **Never run a bare `uv sync`.** Always `uv sync -p /usr/bin/python3.14`.
- **Never `source` this repo's `.env`.** CRLF line endings put a `\r` into
  every value and the Anthropic client reports the resulting illegal header by
  printing the whole API key. Pipe anything that could surface one through
  `sed -E 's/sk-ant-[A-Za-z0-9_-]+/sk-ant-***/g'`.
- **Never dump `docker compose config --format json`** — it prints every
  resolved secret. Never `docker compose down -v` against prod.
- **`ruff check` and `ruff format --check` must be green at every task gate.**
  An unused `# noqa` is an error here (`RUF100`), `BLE` is not an enabled rule
  set (write the reason for a broad `except` as a plain comment), and a
  **malformed** `# noqa` is reported as a `warning:` line while `ruff check`
  still exits 0 — read the lines above the summary, not just the exit code.
- **`uv run ty check` is deferred to Task 5** — see *The red window* below.
- **Pass `verdict` explicitly at every call site that writes an entry.** A
  permissive default on the column that selects the queue silently re-admits
  everything; this is `db9de98`, the trap this repo is most likely to repeat.
- **Every read wraps in its own `session.begin()`.** In SQLAlchemy 2 a bare
  read autobegins, so a later `session.begin()` raises *A transaction is
  already begun*. The suite structurally cannot catch this: the hand-written
  fake sessions yield from `begin()` unconditionally.
- **Never reach a relationship by attribute access on an `AsyncSession`.** A
  lazy load raises `MissingGreenlet` at the attribute, which mentions neither
  commits nor transactions, and no fake session sees it. Load related rows with
  an explicit query.
- **Nothing may precede the commit on the ingest path.** aiogram advances the
  polling offset as it dispatches, so an update lost mid-handler is never
  redelivered. Commit first with a safe default, make the model call outside
  any transaction, refine in a second short transaction.
- **A test that passes against deliberately broken code is an ordinary outcome
  here.** Every task's "run it and watch it fail" step is mandatory, and the
  failure must be *for the reason expected* — an import error or a fake that
  never reaches the assertion has not discriminated.
- **`PROMPT_VERSION` is not bumped.** No prompt changes meaning in this work.
- Commit on `metheoryt/v2`. Never edit the main checkout.

## The red window

There is no decomposition in which every task leaves a green suite. Task 2
moves columns that Tasks 3–5 still reference; the references are inside
function bodies, so nothing fails at import — the named test files fail at
call time. That is deliberate and bounded:

| task | full suite | what is expected red | `ty check` |
|---|---|---|---|
| 1 | **green** | — | green |
| 2 | red | `test_store.py`, `test_ingest.py`, `test_extract.py`, `test_qhandler.py`, `test_routing.py`, `test_outbound.py` | **not run** |
| 3 | red | `test_ingest.py`, `test_extract.py`, `test_qhandler.py`, `test_routing.py` | **not run** |
| 4 | red | `test_extract.py` | **not run** |
| 5 | **green** | — | green |

A reviewer gates a task on *its own* named test files being green and on that
row's red list being exactly what is red — no more. Task 5 closes the window
and is the first gate that runs `uv run ty check`.

## File Structure

**Modified**

- `telegrind/models.py` — add `Entry` and `SOURCE_TELEGRAM`; strip five
  columns from `LoggedMessage`; `Fact.message_pk` → `Fact.entry_pk`.
- `telegrind/store.py` — `upsert_message` writes both rows; the queue helpers
  select `Entry`; the fact helpers key on `entry_pk`; two new lookups.
- `telegrind/extract.py` — the window is entries; messages are loaded
  explicitly for the prompt.
- `telegrind/bot/handlers/handlers.py`, `telegrind/bot/handlers/query.py`,
  `telegrind/bot/routing.py`, `telegrind/bot/handlers/reactions.py` — the
  verdict is read off the entry.
- `CLAUDE.md`, `.claude/memory/project.md` — the architecture prose.

**Created**

- `alembic/versions/<generated>_entries.py` — the eighth revision.

**Deleted**

- `telegrind/import_history.py`, `tests/test_import_history.py`,
  `telegrind/workbook_compare.py`, `tests/test_workbook_compare.py`.

**Untouched, and re-verified at Task 5:** `telegrind/query.py`,
`telegrind/answer.py`, `telegrind/taxonomy.py`, `telegrind/coerce.py`,
`telegrind/classify.py`, `telegrind/llm.py`, `telegrind/config.py`.

## Rulings carried from the spec review

Two things in the spec do not survive contact with the code. Both are settled
here so no implementer has to guess.

1. **"`BATCH = 20` stays" (spec §The extraction queue) is a leftover.**
   `BATCH` is defined once, at `telegrind/import_history.py:358`, and that
   module is deleted at Task 1. The correctness bound the sentence describes is
   real and unchanged — `extract._pass` makes one model call for the whole tail
   against `llm.MAX_TOKENS = 2048`, and `llm.use_tool` never inspects
   `response.stop_reason`, so a truncated reply still stamps the whole tail as
   extracted. What actually carries that risk in live code is
   `extract.run`'s `limit=200` default, which this work does not change. The
   constant dies with its module; no new constant replaces it.
2. **Spec decision 8 rests on "there is exactly one writer" of the message
   text.** Verified 2026-09-17: nothing writes `transcript` or
   `transcript_model` anywhere — `store.message_values` always writes `None`
   and `upsert_message` only guards against clobbering a value no code
   produces. So `entry.content` has exactly one writer today. Task 3 records
   the obligation in a docstring: **a future transcription path must update
   `entry.content` and clear `entry.extracted_at` in the same transaction as
   the transcript**, or voice entries go stale and the queue skips them
   silently.

---

### Task 1: Retire the parked modules

Deletion first: `workbook_compare.py` is the only consumer of
`Fact.message_pk` outside `store`/`extract`, and `import_history.py` is the
only other caller of `upsert_message`. Removing them now takes ~700 lines and
two test files out of every later diff.

**Files:**
- Delete: `telegrind/import_history.py`
- Delete: `tests/test_import_history.py`
- Delete: `telegrind/workbook_compare.py`
- Delete: `tests/test_workbook_compare.py`
- Modify: `CLAUDE.md` — the `telegrind/import_history.py` paragraph under
  *Key layers* (it begins ``**`telegrind/import_history.py`** — the one-time
  v1 import`` and ends with the link to
  `docs/superpowers/specs/2026-09-16-history-import-design.md`)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. After this task no module imports `import_history` or
  `workbook_compare`.

- [ ] **Step 1: Confirm nothing else references them**

```bash
grep -rn "import_history\|workbook_compare" --include='*.py' telegrind tests main.py
```

Expected: only the four files about to be deleted. If anything else appears,
stop and report it — the plan assumed otherwise.

- [ ] **Step 2: Delete the four files**

```bash
git rm telegrind/import_history.py tests/test_import_history.py \
       telegrind/workbook_compare.py tests/test_workbook_compare.py
```

- [ ] **Step 3: Remove the `import_history.py` paragraph from `CLAUDE.md`**

Delete the whole bullet, from ``**`telegrind/import_history.py`** — the
one-time v1 import, and the only`` through ``Design:
`docs/superpowers/specs/2026-09-16-history-import-design.md`.`` Leave the
neighbouring bullets (`telegrind/config.py`, `telegrind/models.py`) untouched.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest`
Expected: PASS, with the two deleted test files no longer collected. Note the
test count — it is the baseline the later tasks are measured against.

- [ ] **Step 5: Lint**

Run: `uv run ruff check && uv run ruff format --check && uv run ty check`
Expected: all three PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: retire the parked import modules

Both are superseded by the entries design: import_history.py imports the
Telegram export as messages, which decision 4 does not want built, and
workbook_compare.py joins on a message id the real export does not carry.
Recoverable from git if either job is ever wanted."
```

---

### Task 2: `entry` — the model and the migration

**Files:**
- Modify: `telegrind/models.py`
- Create: `alembic/versions/<generated>_entries.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces, for every later task:
  - `telegrind.models.Entry` with columns `id`, `chat_pk`, `source`,
    `external_id`, `message_pk`, `occurred_at`, `content`, `raw`, `verdict`,
    `extracted_at`, `extract_model`, `extract_prompt_version`,
    `extract_error`, `created_at`.
  - `telegrind.models.SOURCE_TELEGRAM = "telegram"`.
  - `LoggedMessage` **no longer has** `verdict`, `extracted_at`,
    `extract_model`, `extract_prompt_version`, `extract_error`. It keeps
    `receipt_emoji` and the `content` property.
  - `Fact.entry_pk: Mapped[int]`, NOT NULL, FK `entry.id` `ON DELETE CASCADE`.
  - The partial unique index is named `uq_fact_entry_pk_seq_live`.

- [ ] **Step 1: Write the failing tests**

Replace `test_message_carries_extraction_state`,
`test_a_new_message_is_unextracted` and any other test in
`tests/test_models.py` asserting the message's extraction columns or
`Fact.message_pk` with these. Keep every other test in the file.

```python
from telegrind.models import SOURCE_TELEGRAM, Entry


def test_entry_table_name() -> None:
    assert Entry.__tablename__ == "entry"


def test_the_message_no_longer_carries_derivation_state() -> None:
    """`message` is the verbatim log and nothing else.

    Two columns that can disagree about whether something gets extracted is
    the `extractable`-vs-`verdict` defect this repo already shipped, so the
    old ones must be gone, not merely unread.
    """
    columns = set(LoggedMessage.__table__.columns.keys())
    assert not columns & {
        "verdict",
        "extracted_at",
        "extract_model",
        "extract_prompt_version",
        "extract_error",
    }
    assert "receipt_emoji" in columns


def test_entry_carries_the_verdict_and_the_extraction_state() -> None:
    columns = set(Entry.__table__.columns.keys())
    assert {
        "chat_pk",
        "source",
        "external_id",
        "message_pk",
        "occurred_at",
        "content",
        "raw",
        "verdict",
        "extracted_at",
        "extract_model",
        "extract_prompt_version",
        "extract_error",
        "created_at",
    } <= columns


def test_a_side_loaded_entry_has_no_message() -> None:
    """The whole point: a fact need not come from Telegram."""
    assert Entry.__table__.c.message_pk.nullable is True
    assert Entry.__table__.c.content.nullable is True
    assert Entry.__table__.c.raw.nullable is True


def test_an_entry_is_unique_per_source_row() -> None:
    """This is what makes a re-import idempotent rather than doubling."""
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Entry.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_pk", "external_id", "source") in constraints


def test_one_entry_per_message() -> None:
    """Partial, because most entries have no message and NULLs collide badly."""
    index = next(i for i in Entry.__table__.indexes if i.name == "uq_entry_message_pk")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["message_pk"]
    assert index.dialect_options["postgresql"]["where"] is not None


def test_the_queue_has_an_index() -> None:
    index = next(i for i in Entry.__table__.indexes if i.name == "ix_entry_queue")
    assert [c.name for c in index.columns] == [
        "chat_pk",
        "verdict",
        "extracted_at",
        "occurred_at",
    ]


def test_a_new_entry_is_unextracted() -> None:
    row = Entry(
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id="2",
        occurred_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )
    assert row.extracted_at is None
    assert row.extract_error is None
    assert row.verdict == "fact"


def test_a_fact_hangs_off_an_entry() -> None:
    assert "message_pk" not in Fact.__table__.columns
    assert Fact.__table__.c.entry_pk.nullable is False
    target = next(iter(Fact.__table__.c.entry_pk.foreign_keys)).target_fullname
    assert target == "entry.id"


def test_the_live_uniqueness_moved_to_the_new_column() -> None:
    """Created anew, never renamed: an index renamed onto a different column
    guards nothing and fails silently."""
    names = {i.name for i in Fact.__table__.indexes}
    assert "uq_fact_entry_pk_seq_live" in names
    assert "uq_fact_message_pk_seq_live" not in names
    index = next(i for i in Fact.__table__.indexes if i.name == "uq_fact_entry_pk_seq_live")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["entry_pk", "seq"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Entry'` on collection.

- [ ] **Step 3: Add `Entry` to `telegrind/models.py`**

Put `SOURCE_TELEGRAM` beside the `VERDICT_*` constants, and the `Entry` class
between `LoggedMessage` and `Fact`.

```python
#: Where an entry came from. A string, not an FK: a table earns its place
#: when sources acquire configuration (per-bank column mappings), and
#: promoting a string to an FK later is a migration, not a redesign.
SOURCE_TELEGRAM = "telegram"
```

```python
class Entry(Model):
    """The unit that yields facts. A Telegram message is one kind of entry.

    The log was built on the assumption that a fact is derived from a
    message, and that assumption is wrong in two directions: the v1
    workbook is already structured, and receipts and bank statements are
    coming. So the derivation state that used to sit on `message` — the
    verdict and the four extraction columns — sits here instead, and
    `message` goes back to being the verbatim record of what Telegram sent.

    `source` is also the undo. Everything one import wrote is
    `WHERE source = '<that source>'`, which is why no import-run record
    exists.
    """

    __tablename__ = "entry"
    __table_args__ = (
        UniqueConstraint(
            "chat_pk", "source", "external_id", name="uq_entry_chat_source_external"
        ),
        #: Partial, because most entries have no message: Postgres treats
        #: NULLs as distinct, so a total unique index would admit any number
        #: of message-less entries and then mean nothing.
        Index(
            "uq_entry_message_pk",
            "message_pk",
            unique=True,
            postgresql_where=text("message_pk IS NOT NULL"),
        ),
        Index("ix_entry_queue", "chat_pk", "verdict", "extracted_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_pk: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))
    source: Mapped[str]
    #: The source's own key, as text. Telegram: `str(message_id)`. A sheet
    #: row: its column-A key. A statement: the transaction id.
    external_id: Mapped[str]
    #: Set only for chat-borne entries. Who wrote a message and what it
    #: replies to are Telegram facts, read through here rather than copied.
    message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), default=None
    )
    #: When the thing happened: `tg_date` for a message, the row's own date
    #: for an import.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: What the extractor reads. NULL for a structured entry, which yielded
    #: its facts on arrival and needs no model.
    content: Mapped[str | None] = mapped_column(default=None)
    #: The source row verbatim, for imported entries; NULL for chat entries,
    #: whose verbatim copy is `message.raw`. Deliberately never shown to the
    #: model — `taxonomy.observed` reads `fact.fields` only — so this is
    #: where a source's columns go when they must be preserved but must not
    #: enter the extractor's vocabulary.
    raw: Mapped[dict | None] = mapped_column(JSONB, default=None)
    #: What routing decided this is, and therefore what happens to it. Never
    #: null, for the reasons written at VERDICTS. It lives here rather than
    #: on `message` because the extraction state lives here: two flags that
    #: can disagree about whether something gets extracted is the defect
    #: `4d60c7b65ad2` removed.
    verdict: Mapped[str] = mapped_column(default=VERDICT_FACT, server_default="fact")
    extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    extract_model: Mapped[str | None] = mapped_column(default=None)
    extract_prompt_version: Mapped[str | None] = mapped_column(default=None)
    extract_error: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

Then edit `LoggedMessage`: delete the `extracted_at`, `extract_model`,
`extract_prompt_version`, `verdict` and `extract_error` columns **and their
docstring comments**. Keep `receipt_emoji`, keep the `content` property, and
keep the class docstring. Leave `VERDICT_*` and `VERDICTS` where they are —
`Entry` uses them now.

Then edit `Fact`: rename the column and the index.

```python
    entry_pk: Mapped[int] = mapped_column(ForeignKey("entry.id", ondelete="CASCADE"))
```

```python
        Index(
            "uq_fact_entry_pk_seq_live",
            "entry_pk",
            "seq",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
```

Update `Fact`'s class docstring: it says "A replaceable derivation of a
LoggedMessage" — make it "A replaceable derivation of an Entry."

- [ ] **Step 4: Run the model tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Break it once, deliberately, and watch the right test fail**

Temporarily change `postgresql_where=text("message_pk IS NOT NULL")` to
`postgresql_where=None` on `uq_entry_message_pk`.

Run: `uv run pytest tests/test_models.py::test_one_entry_per_message -v`
Expected: FAIL on the `where is not None` assertion — not on a `KeyError`, not
on an import. Then revert the break.

- [ ] **Step 6: Generate the revision**

```bash
uv run alembic revision -m "entries"
```

This writes a file under `alembic/versions/` with a real revision id and sets
`down_revision`. **Confirm `down_revision` is `"4d60c7b65ad2"`** — the current
head. If it is not, stop: another revision landed and the plan is stale.

- [ ] **Step 7: Write the revision body**

Replace the generated `upgrade`/`downgrade` with this, keeping the generated
`revision` / `down_revision` lines exactly as alembic wrote them. Write the
module docstring above them.

```python
"""entries

The unit that yields facts stops being a Telegram message. `entry` carries
the verdict and the extraction state; `message` goes back to being the
verbatim log; `fact` hangs off the entry.

No data is preserved and none is migrated: v2's database is empty — 0 chats,
0 messages, its own volume `telegrind-v2_pgdata` — measured on latitude
2026-09-16, and re-checked immediately before the deploy. v1's database is a
different database on the same host and is untouched.
"""
```

```python
def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_pk", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("message_pk", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.String(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "verdict", sa.String(), nullable=False, server_default="fact"
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extract_model", sa.String(), nullable=True),
        sa.Column("extract_prompt_version", sa.String(), nullable=True),
        sa.Column("extract_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chat_pk"], ["chat.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_pk"], ["message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_pk", "source", "external_id", name="uq_entry_chat_source_external"
        ),
    )
    op.create_index(
        "uq_entry_message_pk",
        "entry",
        ["message_pk"],
        unique=True,
        postgresql_where=sa.text("message_pk IS NOT NULL"),
    )
    op.create_index(
        "ix_entry_queue",
        "entry",
        ["chat_pk", "verdict", "extracted_at", "occurred_at"],
    )

    # The log keeps nothing derived.
    op.drop_column("message", "verdict")
    op.drop_column("message", "extracted_at")
    op.drop_column("message", "extract_model")
    op.drop_column("message", "extract_prompt_version")
    op.drop_column("message", "extract_error")

    # Created anew on the new column, never renamed: an index renamed onto a
    # different column guards nothing and fails silently.
    op.drop_index("uq_fact_message_pk_seq_live", table_name="fact")
    op.drop_constraint("fact_message_pk_fkey", "fact", type_="foreignkey")
    op.drop_column("fact", "message_pk")
    op.add_column("fact", sa.Column("entry_pk", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "fact_entry_pk_fkey", "fact", "entry", ["entry_pk"], ["id"], ondelete="CASCADE"
    )
    op.create_index(
        "uq_fact_entry_pk_seq_live",
        "fact",
        ["entry_pk", "seq"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema.

    Structural only. Facts written against an entry cannot be re-pointed at a
    message — a side-loaded entry has none — so the downgrade drops `fact`'s
    rows with the column. It exists to make the revision reversible on an
    empty database, which is the only database it will ever run against.
    """
    op.drop_index("uq_fact_entry_pk_seq_live", table_name="fact")
    op.drop_constraint("fact_entry_pk_fkey", "fact", type_="foreignkey")
    op.drop_column("fact", "entry_pk")
    op.add_column("fact", sa.Column("message_pk", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "fact_message_pk_fkey",
        "fact",
        "message",
        ["message_pk"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_fact_message_pk_seq_live",
        "fact",
        ["message_pk", "seq"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.add_column(
        "message",
        sa.Column("verdict", sa.String(), nullable=False, server_default="fact"),
    )
    op.add_column(
        "message", sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("message", sa.Column("extract_model", sa.String(), nullable=True))
    op.add_column(
        "message", sa.Column("extract_prompt_version", sa.String(), nullable=True)
    )
    op.add_column("message", sa.Column("extract_error", sa.String(), nullable=True))

    op.drop_index("ix_entry_queue", table_name="entry")
    op.drop_index("uq_entry_message_pk", table_name="entry")
    op.drop_table("entry")
```

The imports at the top of the file are
`import sqlalchemy as sa`, `from sqlalchemy.dialects import postgresql`,
`from alembic import op`, `from collections.abc import Sequence` — match the
style of `alembic/versions/20260911071828_dialogue_first.py`.

**`fact_message_pk_fkey` is a guess and must not be used until it is
confirmed.** Postgres auto-names the constraint, and the revision that created
it passed `sa.ForeignKeyConstraint` with no name, so the default *should*
apply — but a wrong name here fails the migration halfway through, after the
message columns are already dropped. Run this first, and **if it returns a
different name, use that one; do not proceed on the guess**:

```bash
docker compose up -d postgres
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
load_dotenv('.env')
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c:
        r = await c.execute(text(
            \"select conname from pg_constraint where conrelid = 'fact'::regclass and contype = 'f'\"
        ))
        print(list(r))
asyncio.run(main())
" 2>&1 | sed -E 's/sk-ant-[A-Za-z0-9_-]+/sk-ant-***/g'
```

If the name differs, use the real one.

- [ ] **Step 8: Run the migration against the dev database, both ways**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: all four succeed. Then confirm the shape:

```bash
docker compose exec -T postgres psql -U telegrind -d telegrind \
  -c '\d entry' -c '\d message' -c '\d fact'
```

Expected: `entry` exists with both unique constraints and the queue index;
`message` has none of the five dropped columns; `fact` has `entry_pk` and
`uq_fact_entry_pk_seq_live`.

- [ ] **Step 9: Confirm the red window is exactly what the table says**

Run: `uv run pytest`
Expected: `tests/test_models.py`, `test_answer.py`, `test_classify.py`,
`test_coerce.py`, `test_config.py`, `test_harness.py`, `test_llm.py`,
`test_query.py`, `test_reactions.py`, `test_setup.py`, `test_taxonomy.py`
PASS; `test_store.py`, `test_ingest.py`, `test_extract.py`, `test_qhandler.py`,
`test_routing.py`, `test_outbound.py` FAIL. Anything red outside that list is a
finding, not an expected failure — report it.

Run: `uv run ruff check && uv run ruff format --check`
Expected: PASS. (`ty check` is deferred to Task 5 — do not run it as a gate.)

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: the entry is the unit that yields facts

message keeps only what Telegram sent; entry carries the verdict and the
extraction state; fact hangs off entry. One revision, no data migration —
v2's database is empty.

Six test modules are knowingly red until the callers move; see the plan's
red-window table."
```

---

### Task 3: `store` speaks entries

**Files:**
- Modify: `telegrind/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Entry`, `SOURCE_TELEGRAM`, `Fact.entry_pk` from Task 2.
- Produces, verbatim:
  - `async upsert_message(session, chat, msg, *, verdict=VERDICT_FACT) -> tuple[LoggedMessage, Entry, bool]`
  - `async get_entry_for_message(session, message_pk: int) -> Entry | None`
  - `async messages_for(session, entries: list[Entry]) -> dict[int, LoggedMessage]` — keyed by **entry id**
  - `async unextracted_tail(session, chat_pk, *, limit=200) -> list[Entry]`
  - `async context_before(session, chat_pk, pivot: Entry, *, limit=10) -> list[Entry]`
  - `async live_facts_for_entry(session, entry_pk: int) -> list[Fact]`
  - `async tombstone_facts(session, entry_pk: int, at: datetime) -> int`
  - `async restore_facts(session, entry_pk: int) -> int`
  - `mark_extracted(rows: list[Entry], *, model, prompt_version, at) -> None`
  - `mark_failed(rows: list[Entry], error: str) -> None`
  - `async replace_facts(session, chat_pk: int, entry_pk: int, drafts, *, model, prompt_version, now) -> int`
  - **Deleted:** `facts_for_message`, `facts_for_chat`. Both are dead after
    Task 1 — `workbook_compare` was their only caller. Verify with
    `grep -rn "facts_for_message\|facts_for_chat" telegrind tests` before
    deleting; if anything calls them, rename rather than delete and report it.

- [ ] **Step 1: Write the failing tests**

In `tests/test_store.py`, extend the fake and add these. The existing
`FakeSession` returns the same rows for every `execute`, which is not enough
for `upsert_message` any more — it now issues two different selects. Replace
it with a queue-driven one and keep the old name so the rest of the file
compiles:

```python
class FakeSession:
    """Enough of AsyncSession for the store helpers.

    `rows` is the default answer. `answers`, when given, is consumed one
    `execute` at a time — needed since `upsert_message` selects the message
    and then its entry.
    """

    def __init__(
        self, rows: list[object], answers: list[list[object]] | None = None
    ) -> None:
        self.rows = rows
        self.answers = answers
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        if self.answers is not None and self.answers:
            return FakeResult(self.answers.pop(0))
        return FakeResult(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass
```

```python
def entry(**overrides: object) -> Entry:
    base = {
        "id": 11,
        "chat_pk": 1,
        "source": SOURCE_TELEGRAM,
        "external_id": "4821",
        "message_pk": 7,
        "occurred_at": TG_DATE,
        "content": "4500 такси",
        "verdict": VERDICT_FACT,
    }
    base.update(overrides)
    return Entry(**base)


async def test_a_first_sighting_writes_the_message_and_its_entry() -> None:
    """Both rows, one transaction. The entry is what the queue reads, so a
    message stored without one is a message that is never extracted."""
    session = FakeSession([], answers=[[], []])
    chat = Chat(id=1, chat_id=7)

    row, ent, created = await store.upsert_message(
        session, chat, text_message(), verdict=VERDICT_FACT
    )

    assert created is True
    assert row in session.added
    assert ent in session.added
    assert ent.source == SOURCE_TELEGRAM
    assert ent.external_id == "4821"
    assert ent.message_pk is row.id
    assert ent.occurred_at == TG_DATE
    assert ent.content == "4500 такси"
    assert ent.verdict == VERDICT_FACT


async def test_the_external_id_is_the_telegram_message_id_as_text() -> None:
    """`(chat_pk, source, external_id)` is what makes a re-import idempotent,
    and it is text for every source — an int here would be a second spelling."""
    session = FakeSession([], answers=[[], []])
    _, ent, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), text_message(message_id=4821)
    )
    assert ent.external_id == "4821"
    assert isinstance(ent.external_id, str)


async def test_an_edit_puts_the_entry_back_in_the_queue() -> None:
    existing = logged(4821)
    ent = entry(extracted_at=AT, extract_error="boom", content="старый текст")
    session = FakeSession([], answers=[[existing], [ent]])

    _, got, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), text_message(text="4500 такси"), verdict=VERDICT_FACT
    )

    assert created is False
    assert got is ent
    assert ent.extracted_at is None
    assert ent.extract_error is None
    assert ent.content == "4500 такси"


async def test_a_question_writes_its_verdict_onto_the_entry_not_the_message() -> None:
    session = FakeSession([], answers=[[], []])
    row, ent, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), text_message(), verdict=VERDICT_QUESTION
    )
    assert ent.verdict == VERDICT_QUESTION
    assert not hasattr(row, "verdict")


async def test_a_side_loaded_entry_is_in_the_queue() -> None:
    """The design's central claim: there is no second extraction path.

    An imported raw entry has no message and still gets extracted by the
    same pass as a typed one."""
    rows = [entry(id=12, message_pk=None, source="v1-receipts", external_id="r-1")]
    session = FakeSession(rows)

    tail = await store.unextracted_tail(session, chat_pk=1, limit=200)

    assert tail == rows


async def test_the_queue_excludes_a_structured_entry_for_both_reasons() -> None:
    """A substitution, and a deliberate one. The spec asks for each reason to
    be asserted alone; a fake session returns its rows unfiltered, so no unit
    test here can tell «excluded by the stamp» from «excluded by the empty
    content». What it can pin is that both predicates are in the statement.
    The per-reason check is run against a live database in the walkthrough."""
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[-1])

    # the stamp
    assert "entry.extracted_at IS NULL" in rendered
    # the empty content, trimmed
    assert "trim" in rendered.lower()
    assert "entry.content" in rendered
    # and the verdict, which is the third gate
    assert "entry.verdict" in rendered
    # read off `entry` alone: a join to `message` would drop every
    # side-loaded entry silently
    assert "message" not in rendered.lower()


async def test_the_queue_orders_by_when_things_happened() -> None:
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[-1])
    assert "ORDER BY entry.occurred_at, entry.id" in rendered


async def test_messages_for_loads_them_in_one_query_keyed_by_entry() -> None:
    """Explicit, never a relationship attribute: a lazy load on an
    AsyncSession raises MissingGreenlet and no fake session can see it."""
    msg = logged(4821)
    session = FakeSession([msg])

    got = await store.messages_for(session, [entry(id=11, message_pk=msg.id)])

    assert got == {11: msg}
    assert len(session.statements) == 1


async def test_messages_for_asks_nothing_when_no_entry_has_a_message() -> None:
    session = FakeSession([])
    got = await store.messages_for(session, [entry(id=11, message_pk=None)])
    assert got == {}
    assert session.statements == []
```

Also update the existing `fact()` helper to build `entry_pk=7` instead of
`message_pk=7`, and every `tombstone_facts` / `restore_facts` /
`replace_facts` call in the file to pass `entry_pk=`.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `TypeError: cannot unpack` / `AttributeError` on
`Entry`-shaped assertions. Not an `ImportError`: if it is, `Entry` is not
exported and Task 2 is incomplete.

- [ ] **Step 3: Rewrite the store helpers**

Add to the imports: `Entry`, `SOURCE_TELEGRAM`.

```python
def _entry_for(chat: Chat, row: LoggedMessage, verdict: str) -> Entry:
    """A fresh entry for a message row that has just been written.

    `content` duplicates what the extractor reads, so the queue is a single
    indexed read on one table rather than a join that would drop every
    side-loaded entry. There is exactly one writer of that duplicate — this
    module — and it is verified: nothing writes `transcript` anywhere yet.
    **A future transcription path must update `entry.content` and clear
    `entry.extracted_at` in the same transaction as the transcript**, or a
    voice entry goes stale and the queue skips it with no trace.
    """
    return Entry(
        chat_pk=chat.id,
        source=SOURCE_TELEGRAM,
        external_id=str(row.message_id),
        message_pk=row.id,
        occurred_at=row.tg_date,
        content=row.content or None,
        verdict=verdict,
    )


async def get_entry_for_message(
    session: AsyncSession, message_pk: int
) -> Entry | None:
    result = await session.execute(
        select(Entry).where(Entry.message_pk == message_pk)
    )
    return result.scalar_one_or_none()


async def upsert_message(
    session: AsyncSession,
    chat: Chat,
    msg: Message,
    *,
    verdict: str = VERDICT_FACT,
) -> tuple[LoggedMessage, Entry, bool]:
    """Append the message and its entry, or overwrite both on an edit.

    Returns `(message, entry, created)`. One transaction writes both rows,
    which is what keeps the ingest invariant: the row is committed before
    any model call, and an entry that exists is an entry the queue can see.

    An edit overwrites the text, bumps edited_at, and clears the entry's
    extraction state: the text changed, so whatever was extracted from it no
    longer describes it, and clearing extracted_at is what makes the next
    batch pass pick it up again.
    """
    values = message_values(msg)
    existing = await get_message(session, chat.id, msg.message_id)
    if existing is not None:
        # Never clobber a stored transcript with None on a text edit.
        for key, value in values.items():
            if key in ("transcript", "transcript_model") and value is None:
                continue
            setattr(existing, key, value)
        entry = await get_entry_for_message(session, existing.id)
        if entry is None:
            # A message with no entry cannot happen: every writer here makes
            # both. Healing beats raising anyway — a NoResultFound inside
            # `record`'s transaction would roll the message write back and
            # lose the update, which is the one thing this bot promises not
            # to do.
            entry = _entry_for(chat, existing, verdict)
            session.add(entry)
            await session.flush()
            return existing, entry, False
        entry.occurred_at = existing.tg_date
        entry.content = existing.content or None
        entry.verdict = verdict
        entry.extracted_at = None
        entry.extract_error = None
        return existing, entry, False

    row = LoggedMessage(chat_pk=chat.id, **values)
    session.add(row)
    await session.flush()
    entry = _entry_for(chat, row, verdict)
    session.add(entry)
    await session.flush()
    return row, entry, True
```

```python
def _has_content() -> Any:
    """SQL for «this entry has something the extractor can read».

    A photo, a sticker or a location is stored like everything else and
    simply waits. A structured entry has no content at all and is excluded
    here as well as by its extraction stamp — two independent reasons, so
    neither has to be trusted alone.
    """
    return func.nullif(func.trim(Entry.content), "").is_not(None)


async def unextracted_tail(
    session: AsyncSession, chat_pk: int, *, limit: int = 200
) -> list[Entry]:
    """The entries a pass is responsible for, oldest first.

    `entry` alone, never joined to `message`: a join would drop every
    side-loaded entry, which is the whole point of the table.
    """
    result = await session.execute(
        select(Entry)
        .where(
            Entry.chat_pk == chat_pk,
            Entry.verdict == VERDICT_FACT,
            Entry.extracted_at.is_(None),
            _has_content(),
        )
        .order_by(Entry.occurred_at, Entry.id)
        .limit(limit)
    )
    return list(result.scalars())


async def context_before(
    session: AsyncSession,
    chat_pk: int,
    pivot: Entry,
    *,
    limit: int = 10,
) -> list[Entry]:
    """Read-only neighbours shown to the model but never re-extracted.

    Ordered by when things happened, not by `id`: a forward is dated by its
    origin and an imported row by its own date, so the two genuinely differ.
    The comparison is a row comparison so that two entries sharing a second
    still order deterministically.
    """
    result = await session.execute(
        select(Entry)
        .where(
            Entry.chat_pk == chat_pk,
            _has_content(),
            tuple_(Entry.occurred_at, Entry.id) < (pivot.occurred_at, pivot.id),
        )
        .order_by(Entry.occurred_at.desc(), Entry.id.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def messages_for(
    session: AsyncSession, entries: list[Entry]
) -> dict[int, LoggedMessage]:
    """The message rows behind whichever entries have one, keyed by entry id.

    One explicit query, never an attribute on a relationship: a lazy load on
    an AsyncSession raises MissingGreenlet at the attribute access, which
    mentions neither commits nor transactions, and the fake sessions in this
    suite cannot see it.
    """
    by_message_pk = {e.message_pk: e.id for e in entries if e.message_pk is not None}
    if not by_message_pk:
        return {}
    result = await session.execute(
        select(LoggedMessage).where(LoggedMessage.id.in_(by_message_pk))
    )
    return {by_message_pk[row.id]: row for row in result.scalars()}
```

Then, mechanically: delete `facts_for_message` and `facts_for_chat`; rename
`live_facts_for_message` to `live_facts_for_entry` and swap
`Fact.message_pk == message_pk` for `Fact.entry_pk == entry_pk` in it,
`tombstone_facts`, `restore_facts` and `replace_facts`; change the parameter
name to `entry_pk` at each; change `mark_extracted` and `mark_failed` to take
`list[Entry]`; change `replace_facts`'s `Fact(...)` construction to
`entry_pk=entry_pk`. Update the docstrings that say "this message's facts" to
say "this entry's facts", and the `replace_facts` one that mentions
`(message_pk, seq)` to `(entry_pk, seq)`.

Update the module docstring: "Message and fact repository" becomes "Message,
entry and fact repository", and add a line saying that an entry is written
with its message and that nothing here deletes either.

- [ ] **Step 4: Run the store tests**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS.

- [ ] **Step 5: Break it once and watch the right test fail**

Temporarily add `Entry.message_pk.is_not(None)` to `unextracted_tail`'s
`where`.

Run: `uv run pytest tests/test_store.py::test_a_side_loaded_entry_is_in_the_queue -v`
Expected: FAIL because the tail is empty — not because of a TypeError. The
fake returns rows unconditionally, so **if this still passes, the test does
not discriminate and must be rewritten to assert on the rendered statement**
(`assert "message_pk IS NOT NULL" not in str(session.statements[-1])`). Then
revert the break.

- [ ] **Step 6: Gate**

Run: `uv run pytest tests/test_store.py tests/test_models.py tests/test_outbound.py -v`
Expected: PASS. (`outbound.say` ignores the return value, so it comes back
green here without a change.)

Run: `uv run pytest`
Expected: red exactly in `test_ingest.py`, `test_extract.py`,
`test_qhandler.py`, `test_routing.py`.

Run: `uv run ruff check && uv run ruff format --check`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: store writes and reads entries

upsert_message writes the message and its entry in one transaction and
returns both. The queue selects entry alone — a join to message would drop
every side-loaded entry. facts_for_message and facts_for_chat go: their only
caller was workbook_compare."
```

---

### Task 4: The callers — ingest, /q, routing, reactions

**Files:**
- Modify: `telegrind/bot/handlers/handlers.py`
- Modify: `telegrind/bot/handlers/query.py`
- Modify: `telegrind/bot/routing.py`
- Modify: `telegrind/bot/handlers/reactions.py`
- Test: `tests/test_ingest.py`, `tests/test_qhandler.py`,
  `tests/test_routing.py`, `tests/test_reactions.py`

**Interfaces:**
- Consumes: everything Task 3 produced.
- Produces:
  - `async route(message, entry: Entry, chat, config, session, bot, *, receipt: str = RECEIPT_EMOJI) -> None`
    — `row: LoggedMessage` is **dropped from the signature**. `_act` read it
    only for `row.verdict`, which is now the entry's; everything else it needs
    comes off the aiogram `message`.
  - `async _act(message, entry, chat, config, session, bot, *, receipt) -> None`
    — same change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reactions.py` — this is the hop the spec singles out,
because it is new and can break silently:

```python
class FakeSession:
    """Two selects: the message, then its entry."""

    def __init__(self, answers: list[list[object]]) -> None:
        self.answers = answers
        self.statements: list[object] = []

    def begin(self) -> "FakeSession":
        return self

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        rows = self.answers.pop(0) if self.answers else []
        return SimpleNamespace(
            scalars=lambda: rows,
            scalar_one_or_none=lambda: rows[0] if rows else None,
        )


async def test_a_reaction_tombstones_the_facts_of_the_message_s_entry() -> None:
    """The gesture now walks message → entry → facts, and the middle hop is
    where it can go quiet: a missed entry tombstones nothing and reports
    nothing, and the bubble on screen still says the facts are gone."""
    msg = LoggedMessage(id=7, chat_pk=1, message_id=1072, kind="text", raw={})
    ent = Entry(id=11, chat_pk=1, source=SOURCE_TELEGRAM, external_id="1072", message_pk=7, occurred_at=AT)
    live = [Fact(chat_pk=1, entry_pk=11, seq=1, kind="expense", at=AT, fields={})]
    session = FakeSession([[msg], [ent], live])

    await toggle_delete(event([], ["💔"]), Chat(id=1, chat_id=3260987), session)

    assert live[0].deleted_at is not None


async def test_removing_the_reaction_restores_them_through_the_same_hop() -> None:
    msg = LoggedMessage(id=7, chat_pk=1, message_id=1072, kind="text", raw={})
    ent = Entry(id=11, chat_pk=1, source=SOURCE_TELEGRAM, external_id="1072", message_pk=7, occurred_at=AT)
    dead = [Fact(chat_pk=1, entry_pk=11, seq=1, kind="expense", at=AT, fields={}, deleted_at=AT)]
    session = FakeSession([[msg], [ent], dead])

    await toggle_delete(event(["💔"], []), Chat(id=1, chat_id=3260987), session)

    assert dead[0].deleted_at is None
```

And one for the spec's fourth pinned item — that no call site leans on the
verdict default. Put it in `tests/test_ingest.py`:

```python
def test_every_entry_writing_call_site_passes_a_verdict() -> None:
    """A permissive default on the column that selects the queue silently
    re-admits everything the old flag excluded, and nothing fails — the rows
    simply get parsed. That is `db9de98`, and it cost a live chat.

    Read as source rather than executed: the four call sites are in three
    modules and two of them are only reachable through aiogram.
    """
    import ast
    import pathlib

    sites = 0
    for path in pathlib.Path("telegrind").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "upsert_message":
                continue
            sites += 1
            assert any(kw.arg == "verdict" for kw in node.keywords), (
                f"{path}: upsert_message without an explicit verdict"
            )
    # Four today: handlers.record, handlers.record_edited, query.ask,
    # outbound.say. The floor is here only so that a broken walk finding
    # nothing cannot pass as «every call site is fine»; the keyword is what
    # this test pins, not the census.
    assert sites >= 4, f"the AST walk found only {sites} call sites"
```

Then update the existing fakes: every `test_ingest.py` /
`test_qhandler.py` / `test_routing.py` fake whose `upsert_message` returns a
2-tuple returns a 3-tuple, and every `route(...)` call and `row(...)` helper
passes an `Entry` where it passed a `LoggedMessage`. In `test_routing.py`,
`row(VERDICT_SYSTEM)` becomes an `Entry` factory.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_reactions.py tests/test_ingest.py -v`
Expected: FAIL — the reaction tests on `Fact(... entry_pk=...)` reaching a
handler that still calls `tombstone_facts(session, row.id, ...)`, and the
call-site test on `sites == 3` finding call sites without a `verdict` keyword
or a different count. A `sites == 0` result means the AST walk is wrong, not
that the code is right — fix the test.

- [ ] **Step 3: Move the verdict onto the entry in `handlers.py`**

`_continues` now answers with the root **entry's** verdict:

```python
async def _continues(
    session: AsyncSession, chat_pk: int, message: Message
) -> str | None:
    """The verdict of the turn this message replies into, or None.

    Read *after* the row is committed, never before: a lookup that fails
    ahead of the commit loses the update, and this one is a refinement of
    the routing decision, not part of storing anything.

    In a transaction of its own, because a bare read autobegins one that
    never closes and the next `session.begin()` then raises «a transaction
    is already begun».
    """
    parent = message.reply_to_message
    if parent is None:
        return None
    async with session.begin():
        root_id = await store.turn_root(session, chat_pk, parent.message_id)
        root = await store.get_message(session, chat_pk, root_id)
        if root is None:
            return None
        entry = await store.get_entry_for_message(session, root.id)
        return entry.verdict if entry is not None else None
```

In `record`:

```python
    async with session.begin():
        row, entry, _ = await store.upsert_message(
            session, chat, message, verdict=VERDICT_FACT
        )
        row.receipt_emoji = RECEIPT_EMOJI

    verdict = await classify.verdict_for(
        message.text or message.caption,
        continues=await _continues(session, chat.id, message),
    )

    if verdict != VERDICT_FACT:
        async with session.begin():
            entry.verdict = verdict
            row.receipt_emoji = None

    await route(message, entry, chat, config, session, bot)
```

In `record_edited`, the first block reads the entry as well:

```python
    async with session.begin():
        previous = await store.get_message(session, chat.id, edited_message.message_id)
        previous_entry = (
            await store.get_entry_for_message(session, previous.id)
            if previous is not None
            else None
        )
        was_extracted = (
            previous_entry is not None and previous_entry.extracted_at is not None
        )
        was_fact = previous_entry is not None and previous_entry.verdict == VERDICT_FACT
```

and the third block unpacks three, re-extracts the entry, and tombstones by
entry:

```python
        row, entry, _ = await store.upsert_message(
            session, chat, edited_message, verdict=verdict
        )
        ...
        if was_extracted and verdict == VERDICT_FACT:
            report = await extract.run_for(session, chat, config, entry)
            log.info("re-extracted entry %s: %s fact(s)", entry.id, report.facts)
        elif previous is not None:
            count = await store.tombstone_facts(
                session, entry.id, row.edited_at or datetime.now(UTC)
            )
            log.info("tombstoned %s fact(s) of edited entry %s", count, entry.id)
```

and the final call becomes
`await route(edited_message, entry, chat, config, session, bot, receipt=emoji or RECEIPT_EMOJI)`.

Keep every existing comment in both functions. The two long ones — the
ingest-order rule and the edit race — are still exactly true and are the
reason the shape is what it is.

- [ ] **Step 4: `query.py`**

```python
    async with session.begin():
        row, entry, _ = await store.upsert_message(
            session, chat, message, verdict=VERDICT_QUESTION
        )
        row.receipt_emoji = None

    await route(message, entry, chat, config, session, bot)
```

- [ ] **Step 5: `routing.py`**

Change both signatures from `row: LoggedMessage` to `entry: Entry`, change the
`LoggedMessage` import to `Entry`, and change the two reads in `_act` from
`row.verdict` to `entry.verdict`. Nothing else in the file changes — the
guard, the `parse_mode=None` override and the transaction boundaries all stay
as they are.

Add one line to `route`'s docstring, after "The row is already committed
before this runs": «— the message and its entry both, which is what lets this
read a verdict without a second lookup.»

- [ ] **Step 6: `reactions.py`**

```python
    async with session.begin():
        row = await store.get_message(session, chat.id, message_reaction.message_id)
        if row is None:
            log.info("reaction on unknown message %s", message_reaction.message_id)
            return
        # One hop further than it used to be: facts hang off the entry now.
        # A missing entry is not a normal state — `upsert_message` writes one
        # with every message — so it is logged rather than healed here, where
        # healing would invent an entry with no verdict of its own.
        entry = await store.get_entry_for_message(session, row.id)
        if entry is None:
            log.warning("message %s has no entry", message_reaction.message_id)
            return
        if wants_delete(message_reaction):
            count = await store.tombstone_facts(
                session, entry.id, message_reaction.date
            )
            log.info(
                "tombstoned %s fact(s) of message %s",
                count,
                message_reaction.message_id,
            )
        else:
            count = await store.restore_facts(session, entry.id)
            log.info(
                "restored %s fact(s) of message %s", count, message_reaction.message_id
            )
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_reactions.py tests/test_ingest.py tests/test_qhandler.py tests/test_routing.py tests/test_outbound.py tests/test_setup.py -v`
Expected: PASS.

- [ ] **Step 8: Break it once and watch the right test fail**

Temporarily drop the `verdict=VERDICT_QUESTION` keyword in `query.ask`.

Run: `uv run pytest tests/test_ingest.py::test_every_entry_writing_call_site_passes_a_verdict -v`
Expected: FAIL naming `telegrind/bot/handlers/query.py`. Revert.

Then temporarily change `reactions.toggle_delete` to call
`store.tombstone_facts(session, row.id, ...)` — the message's pk rather than
the entry's.

Run: `uv run pytest tests/test_reactions.py -v`
Expected: FAIL. If it passes, the fake is answering the same rows regardless
of the argument and the test does not discriminate — make the fake key its
answers on the queried pk. Revert.

- [ ] **Step 9: Gate**

Run: `uv run pytest`
Expected: red only in `tests/test_extract.py`.

Run: `uv run ruff check && uv run ruff format --check`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: the verdict is read off the entry

route takes the entry instead of the message row — _act only ever read the
verdict off it. The reaction gesture walks message → entry → facts. The
ingest order is unchanged: both rows commit before the classifier runs."
```

---

### Task 5: `extract` reads entries — the window closes

**Files:**
- Modify: `telegrind/extract.py`
- Test: `tests/test_extract.py`, `tests/test_extraction_quality.py`

**Interfaces:**
- Consumes: `store.unextracted_tail`, `store.context_before`,
  `store.messages_for`, `store.replace_facts(entry_pk=...)` from Task 3.
- Produces:
  - `Draft(entry: Entry, seq, kind, at, fields)` — the field is `entry`, not
    `message`.
  - `build_prompt(tail: list[Entry], context: list[Entry], messages: dict[int, LoggedMessage], taxonomy: str, cfg: ChatConfig, chat_id: int) -> str`
  - `author_of(row: LoggedMessage, chat_id: int) -> str` — unchanged.
  - `async run_for(session, chat, cfg, entry: Entry, *, context_size=10, call=llm.use_tool) -> Report`

- [ ] **Step 1: Write the failing tests**

In `tests/test_extract.py`, add:

```python
async def test_a_side_loaded_entry_is_rendered_without_an_author() -> None:
    """A side-loaded entry has no author and no reply edge, so the prompt
    states neither. Inventing «я» would tell the model a bank statement was
    typed by the user."""
    ent = Entry(
        id=11,
        chat_pk=1,
        source="v1-expenses",
        external_id="4821",
        message_pk=None,
        occurred_at=TG_DATE,
        content="4500 такси",
    )

    prompt = build_prompt([ent], [], {}, "expense (3): amount", CFG, chat_id=OWNER)

    assert "4500 такси" in prompt
    assert "(я)" not in prompt
    assert "ответ на" not in prompt


async def test_a_chat_entry_still_states_its_author_and_reply_edge() -> None:
    """The one thing the hop must not lose: the reply edge is what lets two
    messages give one fact, and it is read off the message, not the entry."""
    parent = logged(10, "хлеб 500")
    child = logged(11, "и молоко 300", raw={"reply_to_message": {"message_id": 10}})
    first = entry(1, "хлеб 500", message_pk=parent.id)
    second = entry(2, "и молоко 300", message_pk=child.id)

    prompt = build_prompt(
        [first, second],
        [],
        {1: parent, 2: child},
        "expense (3): amount",
        CFG,
        chat_id=OWNER,
    )

    assert "(я)" in prompt
    assert "ответ на [1]" in prompt


async def test_the_pass_loads_the_messages_it_needs_in_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never an attribute on a relationship — a lazy load on an AsyncSession
    raises MissingGreenlet at the access, and no fake session here can see
    it. The pass asks `store.messages_for` once, for the tail and the
    context together."""
    calls: list[list[Entry]] = []

    async def fake_messages_for(session: object, entries: list[Entry]) -> dict:
        calls.append(list(entries))
        return {}

    monkeypatch.setattr(store, "messages_for", fake_messages_for)

    tail = [entry(2, "4500 такси")]
    context = [entry(1, "хлеб 500")]

    async def call(
        system: str, user: str, tool: dict, *, model: str | None = None
    ) -> dict:
        return {"facts": []}

    await run(
        FakeWindowSession(tail, context, []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=call,
    )

    assert len(calls) == 1
    assert calls[0] == tail + context
```

This test needs three things `tests/test_extract.py` does not have yet: an
`import pytest`, an `from telegrind import store` (the monkeypatch target must
be the module attribute `extract` reads through, and `extract.py` calls
`store.messages_for`), and an `entry` factory beside the existing `logged`:

```python
def entry(entry_id: int, content: str, *, message_pk: int | None = None) -> Entry:
    return Entry(
        id=entry_id,
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id=str(entry_id),
        message_pk=message_pk,
        occurred_at=datetime(2026, 9, 11, 3, entry_id, tzinfo=UTC),
        content=content,
    )
```

`FakeWindowSession(tail, context, live_facts)` already exists and needs no
change: with `messages_for` patched out the pass issues the same three selects
it always did — the tail, the context, and the taxonomy — and the fake's third
slot answers the taxonomy exactly as it does today.

Update every existing `build_prompt(...)` call in the file for the new
signature, and every `Draft(message=...)` for `Draft(entry=...)`. Then update
the single call in `tests/test_extraction_quality.py:60` —
`build_prompt(rows, [], TAXONOMY, CFG, chat_id=1)` becomes
`build_prompt(entries, [], messages, TAXONOMY, CFG, chat_id=1)`, with the
fixture rows turned into entries carrying `content` and `occurred_at`. That
file is deselected by `addopts` but still **collected**, so a stale call is a
collection error in the default run.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_extract.py -v`
Expected: FAIL on the new signature.

- [ ] **Step 3: Rewrite `extract.py`**

`_line` takes the entry and the message it may not have:

```python
def _line(
    marker: str,
    entry: Entry,
    msg: LoggedMessage | None,
    cfg: ChatConfig,
    chat_id: int,
    markers: dict[int, str],
) -> str:
    """One entry, as the prompt states it.

    The author and the reply edge come off the message, so a side-loaded
    entry states neither rather than guessing — inventing «я» would tell the
    model that a bank statement was typed by the user.
    """
    stamp = cfg.localized(entry.occurred_at).strftime("%Y-%m-%d %H:%M")
    head = f"[{marker}] {stamp}"
    if msg is not None:
        head += f" ({author_of(msg, chat_id)})"
        parent = _reply_to(msg)
        if parent is not None:
            seen = markers.get(parent)
            head += f" → ответ на [{seen}]" if seen else " → ответ на сообщение вне окна"
    return f"{head}: {entry.content or ''}"
```

`build_prompt` keys its markers off the Telegram id of whichever entries have
a message:

```python
def build_prompt(
    tail: list[Entry],
    context: list[Entry],
    messages: dict[int, LoggedMessage],
    taxonomy: str,
    cfg: ChatConfig,
    chat_id: int,
) -> str:
    """The user turn: the taxonomy, the read-only context, the tail.

    `messages` is keyed by entry id and covers the tail and the context
    together — whichever of them came from Telegram. It is built by one
    explicit query in `_pass`, never by touching a relationship.
    """
    markers: dict[int, str] = {}
    for index, row in enumerate(context, 1):
        msg = messages.get(row.id)
        if msg is not None:
            markers[msg.message_id] = f"C{index}"
    for index, row in enumerate(tail, 1):
        msg = messages.get(row.id)
        if msg is not None:
            markers[msg.message_id] = str(index)

    # The `blocks` list that follows is unchanged from the current
    # implementation, word for word. The only edit inside it is that the two
    # `_line(...)` comprehensions now pass `messages.get(row.id)` as the
    # third argument:
    #
    #     _line(f"C{i}", row, messages.get(row.id), cfg, chat_id, markers)
    #     _line(str(i), row, messages.get(row.id), cfg, chat_id, markers)
```

`Draft.message` becomes `Draft.entry: Entry`. In `drafts_from`, `row` is an
entry, `at = to_instant(item.get("when"), cfg, row.occurred_at)`, and the
`Draft(...)` construction uses `entry=row`.

In `_pass`:

```python
    context = await store.context_before(session, chat.id, tail[0], limit=context_size)
    messages = await store.messages_for(session, tail + context)
    vocabulary = taxonomy.render(await taxonomy.observed(session, chat.id))
    prompt = build_prompt(tail, context, messages, vocabulary, cfg, chat.chat_id)
```

and

```python
    for row in tail:
        written += await store.replace_facts(
            session,
            chat_pk=chat.id,
            entry_pk=row.id,
            drafts=[d for d in drafts if d.entry is row],
            model=model,
            prompt_version=llm.PROMPT_VERSION,
            now=now,
        )
```

`run_for`'s parameter becomes `entry: Entry` and its docstring says "one
edited entry". Update the module docstring's first line — "a window of
messages in, facts out" becomes "a window of entries in, facts out" — and add
a sentence: «An entry is usually a message; it can also be a row imported from
a spreadsheet or a statement, and the pass does not know the difference.»

Update the import list: `Entry` joins `Chat` and `LoggedMessage`.

- [ ] **Step 4: Run the extraction tests**

Run: `uv run pytest tests/test_extract.py -v`
Expected: PASS.

- [ ] **Step 5: Break it once and watch the right test fail**

Temporarily change `_line` to always append `f" ({author_of(msg, chat_id)})"`
with a `msg or LoggedMessage(raw={})` fallback.

Run: `uv run pytest tests/test_extract.py::test_a_side_loaded_entry_is_rendered_without_an_author -v`
Expected: FAIL on `"(я)" not in prompt`. Revert.

- [ ] **Step 6: Re-verify the spec's "What is not affected"**

The spec asserts `query.py`, `answer.py` and `taxonomy.py` read `fact` alone
and need no change, verified by reading on 2026-09-16. Re-check now that the
cut has been made:

```bash
grep -n "LoggedMessage\|message_pk\|Entry\|entry_pk\|verdict" \
  telegrind/query.py telegrind/answer.py telegrind/taxonomy.py
```

Expected: no output. Any hit is a finding — report it rather than papering
over it.

- [ ] **Step 7: The window closes**

Run: `uv run pytest`
Expected: PASS, with a test count at or above Task 1's baseline.

Run: `uv run ruff check && uv run ruff format --check && uv run ty check`
Expected: all three PASS. **This is the first gate that runs `ty`** — a
configured tool is not a running tool, so confirm the binary actually executed
and did not silently no-op.

Run: `uv run pytest -m llm --collect-only -q`
Expected: collection succeeds (the cases are not run here; this proves
`test_extraction_quality.py` still imports and calls `build_prompt` correctly).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: the extraction window is entries

The pass reads entry alone and loads the messages behind them in one
explicit query — never through a relationship attribute, which would raise
MissingGreenlet where no fake session can see it. A side-loaded entry states
no author and no reply edge rather than inventing one.

Closes the red window: the full suite and ty are green again."
```

---

### Task 6: The prose — CLAUDE.md and the project memory

The architecture section is read by every agent that opens this repo, and four
of its claims are now false. This is one task rather than four edits folded
into the code tasks, because the prose is cross-cutting and a reviewer should
read it as one piece.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.claude/memory/project.md`

**Interfaces:**
- Consumes: the shipped code from Tasks 2–5.
- Produces: nothing code depends on.

- [ ] **Step 1: Find every claim the cut invalidated**

```bash
grep -n "message.verdict\|message_pk\|extractable\|unextracted_tail\|verdict" CLAUDE.md .claude/memory/project.md
```

Read each hit. The known ones:

- *Key layers* → `telegrind/store.py`: says `upsert_message` clears
  `extracted_at` on the message and that the tail "selects on `verdict`, not
  on `extractable`". Both now describe the entry.
- *Key layers* → `telegrind/models.py`: says `LoggedMessage.verdict` is one of
  four strings and never null, and that the uniqueness is on
  `(message_pk, seq)`. Both moved.
- *Key layers* → `telegrind/extract.py`: "a window of messages".
- The whole section **`## Message routing — the `verdict` column`**: its first
  sentence, "**`message.verdict` — not `extractable` — is what selects the
  extraction tail**", is now wrong in its subject. Rewrite the section around
  `entry.verdict`, **keep** the `db9de98` lesson verbatim (a permissive
  default on a new selector column silently re-admits everything), and keep
  the two `<!-- conflicts-with: -->` and `<!-- src: -->` comments.
- The *Request flow* diagram: `store.upsert_message, COMMIT` now writes two
  rows; say so in the diagram's comment column.

- [ ] **Step 2: Add a `telegrind/models.py` bullet for `Entry`**

Into the *Key layers* list, beside the existing `models.py` bullet — what the
table is, that `source` is also the undo, that `raw` is deliberately not shown
to the model, and that the schema admits imports while no import machinery
exists. Link the spec:
`docs/superpowers/specs/2026-09-16-sources-and-entries-design.md`.

- [ ] **Step 3: Update `.claude/memory/project.md`**

Its `## Ingest invariants that are easy to break` and `## Routing` sections
name `message.verdict`. Correct them the same way. Do not restate the spec —
this file is for what the code does not say.

- [ ] **Step 4: Verify nothing stale is left**

```bash
grep -n "message\.verdict\|uq_fact_message_pk_seq_live\|import_history\|workbook_compare" \
  CLAUDE.md .claude/memory/project.md
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: the verdict column lives on the entry now"
```

---

### Task 7: The one-off v1 expense import, and the walkthrough

**Not shipped code.** The script is written into the session scratchpad, run
against the dev stack, and never committed — spec §The one-off v1 expense
import. What *is* committed from this task is nothing; its deliverable is a
walkthrough report and a working dev database.

**Files:**
- Create (scratchpad, uncommitted): `import_v1_expenses.py`
- Read: `/home/me/Загрузки/Aicha - Expenses.csv` (3548 rows)

**Interfaces:**
- Consumes: the shipped schema and `telegrind.coerce.to_json_value`.
- Produces: nothing the repo depends on.

- [ ] **Step 1: The mapping, verbatim from the spec**

`source = 'v1-expenses'`, `kind = 'expense'`, `entry.content = NULL`,
`entry.message_pk = NULL`, `entry.extracted_at` stamped at import,
`entry.extract_model = NULL`, `entry.verdict = 'fact'`, `entry.raw` = the
whole CSV row as a dict.

| column | destination | note |
|---|---|---|
| `#` | `entry.external_id` | 234…10457, a running counter — **not** a Telegram message id |
| `Сумма` | `fields.amount` | 631 rows use a comma decimal separator |
| `Валюта` | `fields.currency` | upper-cased; one row reads `usd` |
| `Дата` | `entry.occurred_at`, `fact.at` | `DD.MM.YYYY`, no time; midnight at the chat's `tz_offset` |
| `Комментарий` | `fields.comment` | |
| `Авто категория` | `fields.category` | 2129 rows, 20 categories |
| `Необходимость` | `fields.necessity` | the same 2129 rows: must / need / nice / waste |
| `В тенге` | `fields.amount_kzt` | 870 rows are not in KZT |
| `Категория`, `Курс`, `дней назад` | `entry.raw` only | |

Three rules the script obeys: field names are the ones the live extractor
already writes (`amount`, `currency`, `comment`) — a different name would not
sum with an extracted fact; every number goes through
`coerce.to_json_value` so `(fields->>'amount')::numeric` cannot fail a whole
query; dates do **not** go through `coerce.to_instant`, which resolves against
a message's own timestamp, because a sheet row's date is absolute. The fact
carries `model = NULL` and `prompt_version = NULL` and **no source of its
own** — that is one hop away on the entry.

- [ ] **Step 2: Write the script into the scratchpad**

It reads the CSV, opens one `AsyncSession`, and per row writes one `Entry` and
one `Fact` with `seq=1`. Idempotent by `(chat_pk, source, external_id)`: look
the entry up first and update in place rather than inserting. Load the
database URL with `load_dotenv('.env')` and an explicit path — never `source`
the file.

- [ ] **Step 3: Dry-run against the dev database**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run python <scratchpad>/import_v1_expenses.py --dry-run
```

Expected: it reports 3548 rows parsed, 0 unparseable amounts, 0 unparseable
dates, and the currency histogram KZT 2678 / USD 654 / THB 99 / IDR 61 /
VND 49 / RUB 3 / BTC 1 / EUR 1 / CUP 1 — **counted after upper-casing**, so
the one row reading `usd` is inside the 654 and the USD figure the dry-run
prints is **655**. A different total is a finding — stop and report it.

- [ ] **Step 4: Import, then import again**

```bash
uv run python <scratchpad>/import_v1_expenses.py
uv run python <scratchpad>/import_v1_expenses.py
```

```bash
docker compose exec -T postgres psql -U telegrind -d telegrind -c \
  "select count(*) from entry where source = 'v1-expenses'" -c \
  "select count(*) from fact f join entry e on e.id = f.entry_pk where e.source = 'v1-expenses'"
```

Expected: 3548 and 3548 after **both** runs. A doubled count means
`(chat_pk, source, external_id)` is not doing its job.

- [ ] **Step 5: Confirm the undo is one statement**

```bash
docker compose exec -T postgres psql -U telegrind -d telegrind -c \
  "begin; delete from entry where source = 'v1-expenses'; select count(*) from fact; rollback;"
```

Expected: the fact count inside the transaction is the pre-import count — the
`ON DELETE CASCADE` takes the facts with the entries. This is the rollback
decision 4 rests on; verify it once rather than assume it.

- [ ] **Step 6: The manual walkthrough on the dev stack — required**

Every end-to-end walk in this repo's history has found a defect the unit suite
structurally could not see. The fake sessions yield from `begin()`
unconditionally and cannot catch a transaction misuse at all.

```bash
docker compose up
```

In order, against the dev bot:

1. Send `4500 такси`. Expect 💔. Check: one `message` row, one `entry` row
   with `source='telegram'`, `external_id` equal to the Telegram message id,
   `verdict='fact'`, `content='4500 такси'`.
2. Edit it to `5500 такси`. Expect the receipt to advance (💔 → ❤‍🔥). Check
   `entry.extracted_at` is NULL again and `entry.content` is the new text.
3. Send `сколько я потратил на такси` with no `/q`. Expect an answer in words.
4. Tap the 💔 on the expense. Check its facts carry `deleted_at`. Tap again to
   remove the reaction; check `deleted_at` is NULL. **This is the new hop** —
   if it goes quiet, nothing on screen says so.
5. Send `/q сколько я потратил в этом месяце` — expect an answer, and expect
   it to count both the freshly typed expense and the imported ones.
6. Ask for a monthly total that spans the imported range, e.g.
   `сколько я потратил в июне 2024`. Expect a number that matches
   `select sum((fields->>'amount_kzt')::numeric) from fact ...` for that
   month.
7. Send `/start`. Check it lands with `verdict='system'` and never enters the
   tail.

Record the result of each step. A step that cannot be run is a finding.

- [ ] **Step 7: The per-reason exclusion, against a real database**

The unit test in Task 3 can only assert that both predicates are in the
statement. Postgres can answer the question the spec actually asked — that a
structured entry is excluded by the stamp alone, and by the empty content
alone:

```bash
docker compose exec -T postgres psql -U telegrind -d telegrind -c "
  with q as (
    select id, extracted_at is null as unstamped,
           nullif(trim(content), '') is not null as readable
    from entry where source = 'v1-expenses' limit 5
  ) select * from q;"
```

Expected: every imported row has `unstamped = f` **and** `readable = f` — each
of the two alone would keep it out of the queue, which is the point. Then
confirm the queue is in fact empty of them:

```bash
docker compose exec -T postgres psql -U telegrind -d telegrind -c \
  "select count(*) from entry where source = 'v1-expenses'
     and verdict = 'fact' and extracted_at is null
     and nullif(trim(content), '') is not null"
```

Expected: 0.

- [ ] **Step 8: Check the taxonomy actually changed**

```bash
docker compose exec -T postgres psql -U telegrind -d telegrind -c \
  "select kind, jsonb_object_keys(fields) k, count(*) from fact where deleted_at is null group by 1,2 order by 3 desc"
```

Expected: `expense` now lists `category` and `necessity` beside `amount`,
`currency`, `comment`. That is the design's intended behaviour change — after
this import the extractor will start filling those two on new expenses. It is
wanted; it arrives silently; confirm it arrived. No new **kind** is seeded:
loans and wishes are not imported, so `wish` stays uncoined.

- [ ] **Step 9: The pre-deploy precondition — before prod, not before dev**

«There is nothing to lose» is the only claim in the design whose failure is
irreversible. Immediately before deploying to latitude, and not earlier:

```bash
ssh latitude "docker compose -p telegrind exec -T postgres psql -U telegrind -d telegrind \
  -c 'select count(*) from message' -c 'select count(*) from fact' \
  -c 'select id, chat_id, sheet_url from chat'"
```

Expected: 0 and 0. `chat.sheet_url` is dead code-wise and is still the only
pointer to the retired workbook — **write its value down before the deploy**.
If either count is not 0, **stop**: the migration drops columns with no
backfill, and this plan assumed an empty database.

- [ ] **Step 10: Report, and do not commit the script**

Confirm `git status` is clean. The script stays in the scratchpad; the spec
calls it throwaway and a second copy in git is a second thing to maintain.

---

## Self-review

**Spec coverage.** §The schema → Task 2. §The paths (from the chat, the delete
gesture, side-loaded raw, side-loaded structured) → Tasks 3 and 4, with the
side-loaded arms pinned by tests in Tasks 3 and 5. §The extraction queue →
Tasks 3 and 5; the `BATCH = 20` sentence is ruled on above. §The one-off v1
expense import → Task 7. §No data migration → Task 2 (one revision, no
backfill) and Task 7 step 9 (the precondition). §What happens to the parked
modules → Task 1. §Testing, all four pinned items → 1 uniqueness: Task 2
step 1 and Task 7 step 4; 2 structured entry excluded for both reasons: Task 3
step 1 pins both predicates and Task 7 step 7 checks each reason alone against
a real database, because a fake session cannot filter; 3 the reaction hop: Task 4 step 1; 4 verdict explicit at every call
site: Task 4 step 1. The manual walkthrough: Task 7 step 6. §Out of scope
carries no tasks, as it says.

**Not in the spec, added here:** the mirror of pinned test 2 — that a
side-loaded entry *does* enter the queue (Task 3). Without it the exclusion
test passes trivially on an implementation that excludes everything.

**Types.** `upsert_message` returns `(LoggedMessage, Entry, bool)` in Task 3
and is unpacked that way in Tasks 4's three call sites; `outbound.say` ignores
the return and needs no edit. `route` takes `Entry` from Task 4 on.
`unextracted_tail`/`context_before` return `list[Entry]` from Task 3 and are
consumed as entries in Tasks 4 (routing's pending count, which only takes
`len`) and 5. `messages_for` is keyed by **entry id** in both its producer and
its consumer. `replace_facts` takes `entry_pk` in Task 3 and is called with it
in Task 5.
