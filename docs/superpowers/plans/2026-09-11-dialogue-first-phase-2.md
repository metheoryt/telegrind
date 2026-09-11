# Dialogue-first, Phase 2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the stored log answer questions — a batch extraction pass that turns a window of messages into facts with a taxonomy observed from the chat's own history, and `/q`, which runs that pass and then answers from the fact table.

**Architecture:** Nothing extracts on receipt. `/q` is the trigger: it runs one pass over the unextracted tail (plus a few already-extracted neighbours as read-only context), then turns the question into a constrained query spec, builds the SQL itself, and asks the model only to render the numbers as prose. An edit of an already-extracted message re-extracts that one message through the same window builder, so it is still read in the company of its neighbours. Arithmetic is never the model's job.

**Tech Stack:** Python 3.14, aiogram 3.27, SQLAlchemy 2 (asyncpg), Alembic, anthropic 0.97, dateparser, pytest with `asyncio_mode = "auto"`, uv.

**Spec:** `docs/superpowers/specs/2026-09-11-dialogue-first-design.md`

## Global Constraints

- **Nothing written is ever lost.** `/q` is stored like any other message before
  it is answered. A fact the model returns for a message outside the window is
  not silently dropped — it is recorded in `extract_error`.
- **`extractable = false` for every slash command, `/q` included.** A question
  must never coin a `kind`, or the observed taxonomy the whole design rests on
  poisons itself. **This includes the edit path.** `COMMAND_LIKE` guards the
  `message` observer only; `edited_message` has one filterless handler, and
  `upsert_message` assigns the flag unconditionally — so a hardcoded `True`
  there silently flips a stored `/q` back on. Editing a typo in a question is
  the most ordinary action there is. Task 9 derives the flag from the edited
  text instead.
- **Registration order is match order, and `handlers/__init__.py` is where it
  is decided.** `handlers.py` ends in `COMMAND_LIKE`, which swallows every
  slash message, so `query.py` must be imported *before* it. This is the Phase 1
  trap that already bit once — the plan's own Task 6 named the wrong file.
- **The reference clock for a message is its own `tg_date`, never the moment of
  extraction.** A pass can run a day after the message; «вчера» must still mean
  the day before it was written. `to_instant(raw, cfg, row.tg_date)` is the
  mechanism and the only one.
- **Arithmetic is ours, not the model's.** The model produces a query spec and,
  separately, prose. Every number in an answer comes out of Postgres.
- **The numeric guard lives at the read boundary, not the write boundary.** The
  spec says an unparseable number is "stored as text under a *different* key".
  **That sentence is superseded.** `to_json_value` is a per-value primitive —
  it cannot see the key it is being stored under, and the free-form taxonomy is
  exactly what forbids a list of "numeric keys" (`{"comment": "такси"}` must
  not become `comment_text`). Instead the SQL builder tests
  `jsonb_typeof(fields->'amount') = 'number'`, which is an *exact* test rather
  than a heuristic, precisely because `to_json_value` already guarantees that
  anything parseable is a real JSON number. `coerce.py` is not touched.
  Measured on the dev database 2026-09-11: `sum(cast(f->>'a' as float))
  filter (where jsonb_typeof(f['a']) = 'number')` over a table holding
  `{"a": 5}`, `{"a": "около 500"}` and `{"b": 1}` returns `5` and does **not**
  raise — Postgres does not evaluate an aggregate's argument for a row the
  FILTER excludes. The whole design rests on that, so it was checked rather
  than assumed.
- **One `PROMPT_VERSION`, and it is extraction's.** It is what `fact.prompt_version`
  records. The query-spec and prose prompts persist nothing and are not versioned.
- **`LLM_MODEL_ESCALATE` stays unused in this phase.** Task 8 corrects its
  `.env.dist` comment rather than leaving the knob describing a feature that
  does not exist.
- Tests are pure unit tests: no live database, no network. Follow the existing
  convention — `SimpleNamespace` fakes for aiogram objects, unattached
  SQLAlchemy model instances, hand-written fake sessions (`tests/test_store.py`
  has `FakeResult`/`FakeSession`). The one exception is the `-m llm` quality
  gate, which is deselected by default.
- Every LLM call goes through a function parameter with a default, so a test
  passes a fake and never touches the network.
- Run tests with `uv run pytest`. Lint with `uv run ruff check` and
  `uv run ruff format`. Type-check with `uv run ty check`. All three must be
  clean at every commit.

## Scope — this is Phase 2 of three

| Phase | Deliverable |
| --- | --- |
| 1 (done) | Sheet layer deleted; store-only ingest; 💔 receipt; reaction-driven soft delete. |
| **2 (this plan)** | Batch extraction over a window with the observed taxonomy, and `/q`. |
| 3 | History import from the Telegram export, and the workbook-as-labelled-set comparison. |

There is no migration in this phase. Phase 1 already shaped both tables.

## File Structure

| File | Responsibility |
| --- | --- |
| Create `telegrind/taxonomy.py` | Read the chat's own `kind`/field vocabulary out of `fact` and render it for a prompt. |
| Create `telegrind/extract.py` | The batch pass: window → prompt → model → drafts → rows, and the marking rules. |
| Create `telegrind/query.py` | The query spec, its validation, and the deterministic spec→SQL→rows builder. |
| Create `telegrind/bot/handlers/query.py` | The `/q` handler: store, pass, spec, numbers, prose. |
| Create `telegrind/bot/handlers/receipts.py` | The receipt emoji, the cycle and `acknowledge` — the half of `handlers.py` that registers nothing, so importing it cannot lose a registration race. |
| Modify `telegrind/llm.py` | Two call helpers (`use_tool`, `say`), the extraction tool schema, the three prompts. |
| Modify `telegrind/store.py` | `unextracted_tail`, `context_before`, `mark_extracted`, `mark_failed`, `replace_facts`. |
| Modify `telegrind/bot/handlers/handlers.py` | `record_edited` re-extracts an already-extracted message. |
| Modify `telegrind/bot/handlers/__init__.py` | Import `query` before `handlers`. |
| Modify `tests/fixtures/extraction.yaml` | `sent:` per row; registry-era `category:` labels dropped. |
| Create `tests/test_taxonomy.py`, `tests/test_extract.py`, `tests/test_query.py`, `tests/test_qhandler.py`, `tests/test_extraction_quality.py` | One per unit; the last is the `-m llm` gate. |

---

### Task 1: The observed taxonomy

The registry is gone; the vocabulary is now whatever the chat's own facts
already use. This task reads it and renders it, and nothing else.

**Files:**
- Create: `telegrind/taxonomy.py`
- Test: `tests/test_taxonomy.py`

**Interfaces:**
- Consumes: `telegrind.models.Fact`.
- Produces: `KindUsage(kind: str, fields: tuple[str, ...], count: int)`;
  `async observed(session: AsyncSession, chat_pk: int) -> list[KindUsage]`;
  `render(usages: list[KindUsage]) -> str`.

- [x] **Step 1: Write the failing test**

```python
# tests/test_taxonomy.py
from types import SimpleNamespace

from telegrind.taxonomy import KindUsage, observed, render


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self._rows)


async def test_observed_groups_fields_under_their_kind():
    session = FakeSession(
        [
            ("expense", "amount", 128),
            ("expense", "comment", 128),
            ("expense", "currency", 120),
            ("loan", "amount", 31),
            ("loan", "counterparty", 31),
        ]
    )

    usages = await observed(session, chat_pk=1)

    assert usages == [
        KindUsage(kind="expense", fields=("amount", "comment", "currency"), count=128),
        KindUsage(kind="loan", fields=("amount", "counterparty"), count=31),
    ]


async def test_observed_orders_kinds_by_how_often_they_occur():
    session = FakeSession([("wish", "text", 3), ("expense", "amount", 128)])

    usages = await observed(session, chat_pk=1)

    assert [u.kind for u in usages] == ["expense", "wish"]


async def test_render_is_one_line_per_kind():
    rendered = render(
        [KindUsage(kind="expense", fields=("amount", "comment"), count=128)]
    )

    assert rendered == "- expense (128): amount, comment"


async def test_render_says_so_when_nothing_has_been_observed_yet():
    assert "пока" in render([])
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_taxonomy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrind.taxonomy'`

- [x] **Step 3: Write the implementation**

```python
# telegrind/taxonomy.py
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
    return "\n".join(
        f"- {u.kind} ({u.count}): {', '.join(u.fields)}" for u in usages
    )
```

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_taxonomy.py -v`
Expected: PASS, 4 tests

- [x] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/taxonomy.py tests/test_taxonomy.py
git commit -m "feat: the taxonomy is observed from the chat's own facts"
```

---

### Task 2: The window

Which messages a pass reads. Two queries, no prompt, no model.

**Files:**
- Modify: `telegrind/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  `async unextracted_tail(session, chat_pk: int, *, limit: int = 200) -> list[LoggedMessage]`
  and
  `async context_before(session, chat_pk: int, pivot: LoggedMessage, *, limit: int = 10) -> list[LoggedMessage]`.
  Both return chat order, oldest first.

**Window rules, settled here so no later task guesses:**

- The tail is `extractable = true`, `extracted_at is null`, **and has content** —
  `coalesce(nullif(trim(transcript), ''), nullif(trim(text), ''))` is not null.
  A photo or a sticker is stored, is never sent to the model, is never marked,
  and does not occupy the window budget. *Not yet parsed* is a third state,
  distinct from both *parsed, yielded nothing* and *failed*.
- Chat order is `(tg_date, message_id)`, not `id`. A forward is dated by its
  origin, so insertion order and chat order genuinely differ.
- Context is the `limit` messages with content strictly before the pivot in
  chat order, whatever their extraction state. It **excludes the tail** by
  construction: the pivot is the tail's first message, so everything before it
  is already extracted or not extractable. On the first-ever pass the context is
  empty, and that is correct rather than a bug.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def logged(message_id: int, *, text: str | None = "x", extracted: bool = False):
    """An unattached message row, enough for the window queries."""
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=datetime(2026, 9, 11, 12, message_id, tzinfo=UTC),
        raw={},
        extracted_at=datetime(2026, 9, 11, tzinfo=UTC) if extracted else None,
    )


async def test_unextracted_tail_asks_for_content_and_chat_order():
    rows = [logged(1), logged(2)]
    session = FakeSession(rows)

    tail = await store.unextracted_tail(session, chat_pk=1, limit=200)

    assert tail == rows
    rendered = str(session.statements[-1])
    assert "extracted_at IS NULL" in rendered
    assert "extractable" in rendered
    assert "trim" in rendered.lower()
    assert "LIMIT" in rendered


async def test_context_before_comes_back_oldest_first():
    # The query walks backwards from the pivot, so the driver hands them
    # back newest-first and the function has to flip them.
    session = FakeSession([logged(9), logged(8)])

    context = await store.context_before(session, chat_pk=1, pivot=logged(10), limit=10)

    assert [row.message_id for row in context] == [8, 9]
```

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py -v -k "tail or context_before"`
Expected: FAIL with `AttributeError: module 'telegrind.store' has no attribute 'unextracted_tail'`

- [x] **Step 3: Write the implementation**

Add to the imports in `telegrind/store.py`: `from sqlalchemy import func, select, tuple_`.

```python
def _has_content() -> Any:
    """SQL for «this message has something the extractor can read».

    A photo, a sticker or a location is stored like everything else and
    simply waits: not yet parsed, which is neither «yielded nothing» nor
    «failed». When something can read a photo, the rows are still here.
    """
    return func.coalesce(
        func.nullif(func.trim(LoggedMessage.transcript), ""),
        func.nullif(func.trim(LoggedMessage.text), ""),
    ).is_not(None)


async def unextracted_tail(
    session: AsyncSession, chat_pk: int, *, limit: int = 200
) -> list[LoggedMessage]:
    """The messages a pass is responsible for, oldest first."""
    result = await session.execute(
        select(LoggedMessage)
        .where(
            LoggedMessage.chat_pk == chat_pk,
            LoggedMessage.extractable.is_(True),
            LoggedMessage.extracted_at.is_(None),
            _has_content(),
        )
        .order_by(LoggedMessage.tg_date, LoggedMessage.message_id)
        .limit(limit)
    )
    return list(result.scalars())


async def context_before(
    session: AsyncSession,
    chat_pk: int,
    pivot: LoggedMessage,
    *,
    limit: int = 10,
) -> list[LoggedMessage]:
    """Read-only neighbours shown to the model but never re-extracted.

    Ordered by chat time, not by `id`: a forward is dated by its origin,
    so the two genuinely differ. The comparison is a row comparison so
    that two messages sharing a second still order deterministically.
    """
    result = await session.execute(
        select(LoggedMessage)
        .where(
            LoggedMessage.chat_pk == chat_pk,
            _has_content(),
            tuple_(LoggedMessage.tg_date, LoggedMessage.message_id)
            < (pivot.tg_date, pivot.message_id),
        )
        .order_by(LoggedMessage.tg_date.desc(), LoggedMessage.message_id.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))
```

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS

- [x] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/store.py tests/test_store.py
git commit -m "feat: the window a pass reads is the tail plus its neighbours"
```

---

### Task 3: The prompt

Pure string assembly — no session, no model. What the extractor sees.

**Files:**
- Create: `telegrind/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `taxonomy.render`, `ChatConfig`, `LoggedMessage`.
- Produces: `author_of(row: LoggedMessage, chat_id: int) -> str`;
  `build_prompt(tail: list[LoggedMessage], context: list[LoggedMessage], taxonomy: str, cfg: ChatConfig, chat_id: int) -> str`.

**Why each piece is there:**

- **Every message states its own local timestamp.** The model resolves nothing
  about time — it copies the phrase the text used into `when` and our code does
  the clock arithmetic against that message's `tg_date`. Stating the timestamp
  anyway is what lets the model tell «в понедельник» (past) from «до понедельника»
  (a due date) and get `when` right at all.
- **Authorship has three arms, not two.** `MessageOriginHiddenUser` carries a
  display name and no id, so "is this me?" has no answer. A rule that branches
  on authorship without the third arm dies on a `None`. Nothing is dropped for
  where it came from — a channel's product post forwarded into the chat is the
  natural way to record a wish.
- **Context is labelled and numbered `C1…`,** so a fact can never be attributed
  to it: only `1…N` are valid targets.
- **A reply names its parent by that parent's marker.** Telegram already
  records the link in `raw["reply_to_message"]`, and it is a far stronger
  signal than adjacency: a photo of a receipt and the price typed under it
  are one purchase however many messages sit between them. Rendering it
  costs one line and reaches the same `row.raw` that `author_of` already
  reads. A parent that fell outside the window is stated as such rather
  than omitted — the model should know it is missing something instead of
  inventing it.
- **A fact assembled from several messages still has exactly one owner** —
  the last message of the group, per the spec's *Attribution*, because
  `unique (message_pk, seq)` forces a single owner. The reply link changes
  which messages the model reads together, not how a fact is stored. Its
  reach is therefore the window: a reply to an older, already extracted
  message produces a *second* fact on the reply rather than completing the
  first one, because extraction state is per-message (`extracted_at`).
  Fixing that is its own phase, not this task.

- [x] **Step 1: Write the failing test**

```python
# tests/test_extract.py
from datetime import UTC, datetime

from telegrind.config import ChatConfig
from telegrind.extract import author_of, build_prompt
from telegrind.models import KIND_TEXT, LoggedMessage

CFG = ChatConfig(tz_offset=6, currency="KZT")
OWNER = 111


def logged(message_id: int, text: str, *, raw: dict | None = None) -> LoggedMessage:
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=datetime(2026, 9, 11, 3, 0, tzinfo=UTC),
        raw=raw or {},
    )


def test_a_plain_message_is_the_owner_talking():
    assert author_of(logged(1, "4500 такси"), OWNER) == "я"


def test_a_forward_of_my_own_message_is_still_me():
    row = logged(
        1, "4500 такси", raw={"forward_origin": {"type": "user", "sender_user": {"id": OWNER}}}
    )

    assert author_of(row, OWNER) == "я"


def test_a_forward_from_someone_else_names_them():
    row = logged(
        1,
        "верни 5000",
        raw={
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 222, "first_name": "Мама"},
            }
        },
    )

    assert author_of(row, OWNER) == "переслано от «Мама»"


def test_a_hidden_sender_is_its_own_third_case():
    row = logged(
        1,
        "верни 5000",
        raw={"forward_origin": {"type": "hidden_user", "sender_user_name": "Мама"}},
    )

    assert "неизвестно" in author_of(row, OWNER)


def test_a_channel_forward_names_the_channel():
    row = logged(
        1,
        "новый монитор",
        raw={"forward_origin": {"type": "channel", "chat": {"title": "Техника"}}},
    )

    assert author_of(row, OWNER) == "переслано из канала «Техника»"


def test_a_reply_names_its_parent_by_marker():
    prompt = build_prompt(
        tail=[
            logged(10, "макбук за 660000"),
            logged(11, "чек", raw={"reply_to_message": {"message_id": 10}}),
        ],
        context=[],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [1]" in prompt


def test_a_reply_to_a_context_message_names_its_context_marker():
    prompt = build_prompt(
        tail=[logged(11, "чек", raw={"reply_to_message": {"message_id": 9}})],
        context=[logged(9, "макбук за 660000")],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [C1]" in prompt


def test_a_reply_to_something_outside_the_window_says_so():
    prompt = build_prompt(
        tail=[logged(11, "чек", raw={"reply_to_message": {"message_id": 3}})],
        context=[],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "вне окна" in prompt


def test_the_prompt_numbers_the_tail_and_labels_the_context():
    prompt = build_prompt(
        tail=[logged(10, "4500 такси"), logged(11, "и ещё 300 кофе")],
        context=[logged(9, "вес 82.4")],
        taxonomy="- expense (5): amount, comment",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "[1]" in prompt and "[2]" in prompt
    assert "[C1]" in prompt
    # The chat's wall clock, not UTC's: 03:00 UTC is 09:00 in Almaty.
    assert "2026-09-11 09:00" in prompt
    assert "- expense (5): amount, comment" in prompt
    assert "KZT" in prompt
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrind.extract'`

- [x] **Step 3: Write the implementation**

```python
# telegrind/extract.py
"""The batch pass: a window of messages in, facts out.

Extraction is deferred so that the model sees a message in the company of
its neighbours — `хлеб 500` / `и молоко 300` is one shopping trip, and
one pass over the whole window converges on one `kind` where N separate
calls coin N synonyms for it.
"""

import logging

from telegrind.config import ChatConfig
from telegrind.models import LoggedMessage

log = logging.getLogger(__name__)


def author_of(row: LoggedMessage, chat_id: int) -> str:
    """Who wrote this, as the prompt states it.

    Three arms, not two. `MessageOriginHiddenUser` carries a display name
    and no id, so "is this me?" has no answer — and every rule that
    branches on authorship dies on that `None` if the third arm is
    missing. Nothing is dropped for where it came from: authorship
    changes what a fact *means*, and meaning is the model's job.
    """
    origin = (row.raw or {}).get("forward_origin")
    if not origin:
        return "я"

    kind = origin.get("type")
    if kind == "user":
        sender = origin.get("sender_user") or {}
        if sender.get("id") == chat_id:
            return "я"
        name = sender.get("first_name") or sender.get("username") or "кто-то"
        return f"переслано от «{name}»"
    if kind == "hidden_user":
        name = origin.get("sender_user_name") or "кто-то"
        return f"переслано от «{name}» (кто именно — неизвестно)"

    title = (origin.get("chat") or {}).get("title") or "без названия"
    where = "канала" if kind == "channel" else "чата"
    return f"переслано из {where} «{title}»"


def _reply_to(row: LoggedMessage) -> int | None:
    """The Telegram message_id this one replies to, if any."""
    return ((row.raw or {}).get("reply_to_message") or {}).get("message_id")


def _line(
    marker: str,
    row: LoggedMessage,
    cfg: ChatConfig,
    chat_id: int,
    markers: dict[int, str],
) -> str:
    stamp = cfg.localized(row.tg_date).strftime("%Y-%m-%d %H:%M")
    head = f"[{marker}] {stamp} ({author_of(row, chat_id)})"
    parent = _reply_to(row)
    if parent is not None:
        seen = markers.get(parent)
        head += f" → ответ на [{seen}]" if seen else " → ответ на сообщение вне окна"
    return f"{head}: {row.content}"


def build_prompt(
    tail: list[LoggedMessage],
    context: list[LoggedMessage],
    taxonomy: str,
    cfg: ChatConfig,
    chat_id: int,
) -> str:
    """The user turn: the taxonomy, the read-only context, the tail."""
    markers: dict[int, str] = {
        row.message_id: f"C{i}" for i, row in enumerate(context, 1)
    }
    markers |= {row.message_id: str(i) for i, row in enumerate(tail, 1)}

    blocks = [
        "# Словарь этого чата",
        "Переиспользуй существующий kind и существующие имена полей, если "
        "подходят. Заводи новые, только если ничего не подходит.",
        taxonomy,
        "",
        f"# Валюта по умолчанию\n{cfg.currency}",
        "",
    ]
    if context:
        blocks += [
            "# Контекст (уже разобран, извлекать из него НЕ надо)",
            "\n".join(
                _line(f"C{i}", row, cfg, chat_id, markers)
                for i, row in enumerate(context, 1)
            ),
            "",
        ]
    blocks += [
        "# Сообщения для разбора",
        "Сообщение, помеченное «ответ на [X]», продолжает сообщение X: читай "
        "их вместе. Если оба здесь и описывают одно и то же — заведи один "
        "факт, а не два.",
        "\n".join(
            _line(str(i), row, cfg, chat_id, markers)
            for i, row in enumerate(tail, 1)
        ),
    ]
    return "\n".join(blocks)
```

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_extract.py -v`
Expected: PASS, 9 tests

- [x] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/extract.py tests/test_extract.py
git commit -m "feat: the extraction prompt states each message's clock, author and reply"
```

---

### Task 4: What comes back

Turning the model's array into coerced drafts. Still pure — no session.

**Files:**
- Modify: `telegrind/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `coerce.to_json_value`, `coerce.to_instant`.
- Produces: `Draft(message: LoggedMessage, seq: int, kind: str, at: datetime, fields: dict[str, object])`;
  `drafts_from(payload: dict, tail: list[LoggedMessage], cfg: ChatConfig) -> tuple[list[Draft], list[str]]`.
  The second element is the list of complaints — things that could not be
  placed. It is never discarded; Task 5 writes it into `extract_error`.

**Settled here:**

- The model returns `when` as the *phrase the text used* («вчера вечером»,
  «15 октября»), or `""` when the text stated no time. Our code resolves it
  against that message's `tg_date`. The model is never asked to do date
  arithmetic, so it cannot get it wrong.
- A fact naming a message index outside `1…len(tail)` is **not silently
  dropped** — it becomes a complaint. A silent drop is invisible in testing and
  violates "nothing written is ever lost" in spirit.
- `seq` is 1-based *within its message*, assigned in the order the model
  returned the facts.

- [x] **Step 1: Write the failing test**

Append to `tests/test_extract.py`:

```python
from telegrind.extract import Draft, drafts_from


def test_a_fact_is_attributed_to_the_message_it_names():
    tail = [logged(10, "4500 такси"), logged(11, "и ещё 300 кофе")]
    payload = {
        "facts": [
            {"message": 2, "kind": "expense", "when": "", "fields": {"amount": "300"}}
        ]
    }

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert complaints == []
    assert len(drafts) == 1
    assert drafts[0].message is tail[1]
    # A number that parses becomes a real JSON number, so `(fields->>…)` works.
    assert drafts[0].fields == {"amount": 300}


def test_an_unparseable_amount_survives_as_text():
    tail = [logged(10, "около 500 на такси")]
    payload = {
        "facts": [
            {
                "message": 1,
                "kind": "expense",
                "when": "",
                "fields": {"amount": "около 500"},
            }
        ]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert drafts[0].fields == {"amount": "около 500"}


def test_when_is_resolved_against_the_messages_own_clock():
    # Sent 2026-09-11 09:00 Almaty. «вчера» is the 10th, not the day the
    # batch pass happens to run.
    tail = [logged(10, "41 бат массаж вчера")]
    payload = {
        "facts": [
            {"message": 1, "kind": "expense", "when": "вчера", "fields": {}}
        ]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert CFG.localized(drafts[0].at).date().isoformat() == "2026-09-10"


def test_a_message_that_states_no_time_is_dated_by_the_message():
    tail = [logged(10, "4500 такси")]
    payload = {"facts": [{"message": 1, "kind": "expense", "fields": {}}]}

    drafts, _ = drafts_from(payload, tail, CFG)

    assert drafts[0].at == tail[0].tg_date


def test_a_fact_pointing_outside_the_window_becomes_a_complaint():
    tail = [logged(10, "4500 такси")]
    payload = {"facts": [{"message": 7, "kind": "expense", "fields": {}}]}

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert drafts == []
    assert len(complaints) == 1
    assert "7" in complaints[0]


def test_a_fact_with_no_kind_becomes_a_complaint():
    tail = [logged(10, "4500 такси")]
    payload = {"facts": [{"message": 1, "kind": "", "fields": {}}]}

    drafts, complaints = drafts_from(payload, tail, CFG)

    assert drafts == []
    assert complaints


def test_seq_restarts_within_each_message():
    tail = [logged(10, "хлеб 500 и молоко 300")]
    payload = {
        "facts": [
            {"message": 1, "kind": "expense", "fields": {"comment": "хлеб"}},
            {"message": 1, "kind": "expense", "fields": {"comment": "молоко"}},
        ]
    }

    drafts, _ = drafts_from(payload, tail, CFG)

    assert [d.seq for d in drafts] == [1, 2]
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_extract.py -v`
Expected: FAIL with `ImportError: cannot import name 'Draft'`

- [x] **Step 3: Write the implementation**

Add to `telegrind/extract.py`:

```python
from dataclasses import dataclass
from datetime import datetime

from telegrind.coerce import to_instant, to_json_value


@dataclass(frozen=True, slots=True)
class Draft:
    """One fact the model returned, coerced but not yet stored."""

    message: LoggedMessage
    seq: int
    kind: str
    at: datetime
    fields: dict[str, object]


def drafts_from(
    payload: dict, tail: list[LoggedMessage], cfg: ChatConfig
) -> tuple[list[Draft], list[str]]:
    """Coerce the model's array. Returns (drafts, complaints).

    A complaint is something that could not be placed. It is returned
    rather than logged and forgotten, because a silently dropped fact is
    invisible in testing and the user is never told.
    """
    drafts: list[Draft] = []
    complaints: list[str] = []
    seen: dict[int, int] = {}

    for item in payload.get("facts") or []:
        index = item.get("message")
        if not isinstance(index, int) or not 1 <= index <= len(tail):
            complaints.append(f"факт указывает на сообщение {index!r} вне окна")
            continue

        kind = str(item.get("kind") or "").strip()
        if not kind:
            complaints.append(f"факт без kind в сообщении {index}")
            continue

        row = tail[index - 1]
        raw_fields = item.get("fields")
        fields = {
            str(key): to_json_value(value)
            for key, value in (raw_fields or {}).items()
        }
        # The model copies the phrase; the clock arithmetic is ours, against
        # the message's own timestamp rather than the moment of the pass.
        at = to_instant(item.get("when"), cfg, row.tg_date)

        seen[index] = seen.get(index, 0) + 1
        drafts.append(
            Draft(message=row, seq=seen[index], kind=kind, at=at, fields=fields)
        )

    return drafts, complaints
```

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_extract.py -v`
Expected: PASS, 16 tests

- [x] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/extract.py tests/test_extract.py
git commit -m "feat: the model's array becomes coerced drafts, or a complaint"
```

---

### Task 5: The pass

Writing the drafts down, marking the messages, and the one function that
ties the window, the prompt, the model and the rows together.

**Files:**
- Modify: `telegrind/store.py`, `telegrind/llm.py`, `telegrind/extract.py`
- Test: `tests/test_store.py`, `tests/test_extract.py`

**Interfaces:**
- Produces in `store`:
  `async replace_facts(session, chat_pk: int, message_pk: int, drafts: list[Draft], *, model: str, prompt_version: str, now: datetime) -> int`,
  `mark_extracted(rows: list[LoggedMessage], *, model: str, prompt_version: str, at: datetime) -> None`,
  `mark_failed(rows: list[LoggedMessage], error: str) -> None`.
- Produces in `llm`: `async use_tool(system: str, user: str, tool: dict, *, model: str | None = None) -> dict`;
  `EXTRACT_TOOL`; `EXTRACT_SYSTEM`; `LLMError`.
- Produces in `extract`: `Report(pending, extracted, facts, failed, complaints)`;
  `async run(session, chat, cfg, *, limit=200, context_size=10, call=llm.use_tool) -> Report`.

**Marking rules — settled here so nobody guesses:**

| Outcome | `extracted_at`, `extract_model`, `extract_prompt_version` | `extract_error` |
| --- | --- | --- |
| The call succeeded | set on **every** message in the tail, including the ones that yielded no facts | cleared |
| The call failed | untouched — the tail stays pending and the next `/q` retries it | set on every message in the tail, which is what makes «N сообщений не удалось разобрать» countable |

A **complaint** (a fact naming a message outside the window) is a pass-level
problem with no message to attach it to, so it does not go into
`extract_error` — that column means *this message's own extraction failed*, and
smearing a pass-level note across every row would make the count above lie. It
is counted in the `Report`, logged at warning, and surfaced in the `/q` reply.
Visibility in the answer the user reads beats visibility in a column they
never will.

**Re-extraction diffs by `seq`.** A draft whose seq already has a live fact
updates it in place; a seq with no draft is tombstoned; a tombstone is never
lifted here — only the user's reaction clears `deleted_at`. The partial unique
index is what makes the insert-over-a-tombstone case work.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
async def test_replace_facts_updates_in_place_and_tombstones_the_surplus():
    live = [fact(seq=1, kind="expense"), fact(seq=2, kind="expense")]
    session = FakeSession(live)
    row = logged(10)
    drafts = [
        Draft(message=row, seq=1, kind="expense", at=AT, fields={"amount": 500}),
    ]

    written = await store.replace_facts(
        session,
        chat_pk=1,
        message_pk=10,
        drafts=drafts,
        model="m",
        prompt_version="v",
        now=AT,
    )

    assert written == 1
    assert live[0].fields == {"amount": 500}
    assert live[0].deleted_at is None
    assert live[1].deleted_at == AT


async def test_replace_facts_adds_a_row_for_a_new_seq():
    session = FakeSession([])
    row = logged(10)
    drafts = [Draft(message=row, seq=1, kind="expense", at=AT, fields={})]

    await store.replace_facts(
        session, chat_pk=1, message_pk=10, drafts=drafts,
        model="m", prompt_version="v", now=AT,
    )

    assert len(session.added) == 1
    assert session.added[0].kind == "expense"


def test_mark_extracted_clears_a_previous_error():
    row = logged(10)
    row.extract_error = "boom"

    store.mark_extracted(
        [row], model="m", prompt_version="v", at=AT
    )

    assert row.extracted_at == AT
    assert row.extract_model == "m"
    assert row.extract_error is None


def test_mark_failed_leaves_the_message_pending():
    row = logged(10)

    store.mark_failed([row], "boom")

    assert row.extracted_at is None
    assert row.extract_error == "boom"
```

`FakeSession` gains an `added` list and an `add` method if it does not have
one already. `AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)`.

Append to `tests/test_extract.py`:

```python
from telegrind.extract import Report, run


class FakeWindowSession:
    """Enough session for `run`: two selects, then adds."""

    def __init__(self, tail, context, live_facts) -> None:
        self.results = [tail, context, live_facts]
        self.added: list[object] = []

    async def execute(self, statement: object):
        rows = self.results.pop(0) if self.results else []
        return SimpleNamespace(scalars=lambda: iter(rows), all=lambda: [])

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


async def test_an_empty_tail_makes_no_call():
    called = False

    async def never(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    report = await run(
        FakeWindowSession([], [], []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=never,
    )

    assert report == Report(pending=0, extracted=0, facts=0, failed=0, complaints=0)
    assert not called


async def test_a_successful_pass_marks_every_message_including_the_silent_ones():
    tail = [logged(10, "4500 такси"), logged(11, "привет")]

    async def call(system, user, tool, *, model=None):
        return {
            "facts": [
                {"message": 1, "kind": "expense", "fields": {"amount": "4500"}}
            ]
        }

    report = await run(
        FakeWindowSession(tail, [], []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=call,
    )

    assert report.extracted == 2
    assert report.facts == 1
    assert all(row.extracted_at is not None for row in tail)
    assert all(row.extract_error is None for row in tail)


async def test_a_failed_call_leaves_the_tail_pending_and_countable():
    tail = [logged(10, "4500 такси")]

    async def boom(system, user, tool, *, model=None):
        raise RuntimeError("503")

    report = await run(
        FakeWindowSession(tail, [], []),
        chat=SimpleNamespace(id=1, chat_id=OWNER),
        cfg=CFG,
        call=boom,
    )

    assert report.failed == 1
    assert report.extracted == 0
    assert tail[0].extracted_at is None
    assert "503" in tail[0].extract_error
```

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_extract.py tests/test_store.py -v`
Expected: FAIL with `ImportError: cannot import name 'run'` / `AttributeError: … 'replace_facts'`

- [x] **Step 3: Write the store half**

```python
# telegrind/store.py
def mark_extracted(
    rows: list[LoggedMessage], *, model: str, prompt_version: str, at: datetime
) -> None:
    """A message the pass handled, whether or not it yielded a fact.

    Marking the silent ones is the whole point of the column: without it
    «привет» is indistinguishable from «not yet parsed» and every pass
    re-feeds it forever.
    """
    for row in rows:
        row.extracted_at = at
        row.extract_model = model
        row.extract_prompt_version = prompt_version
        row.extract_error = None


def mark_failed(rows: list[LoggedMessage], error: str) -> None:
    """The pass could not read these. They stay pending and are retried.

    Dropping the echo removed the only channel through which a failure
    reached the user, so it has to be countable here instead.
    """
    for row in rows:
        row.extract_error = error


async def replace_facts(
    session: AsyncSession,
    chat_pk: int,
    message_pk: int,
    drafts: list["Draft"],
    *,
    model: str,
    prompt_version: str,
    now: datetime,
) -> int:
    """Diff this message's facts against what the pass just derived.

    Unchanged rows are left alone, changed ones updated in place, surplus
    ones tombstoned. A tombstone is never lifted here — only the user's
    reaction clears `deleted_at` — which is why inserting over a
    tombstoned `(message_pk, seq)` has to work, and why the uniqueness on
    it is a partial index.
    """
    live = {row.seq: row for row in await live_facts_for_message(session, message_pk)}

    for draft in drafts:
        row = live.pop(draft.seq, None)
        if row is None:
            session.add(
                Fact(
                    chat_pk=chat_pk,
                    message_pk=message_pk,
                    seq=draft.seq,
                    kind=draft.kind,
                    at=draft.at,
                    fields=draft.fields,
                    model=model,
                    prompt_version=prompt_version,
                )
            )
            continue
        row.kind = draft.kind
        row.at = draft.at
        row.fields = draft.fields
        row.model = model
        row.prompt_version = prompt_version

    for surplus in live.values():
        surplus.deleted_at = now

    return len(drafts)
```

`extract` imports `store`, so `store` must not import `extract` at runtime.
Put `Draft` behind `if TYPE_CHECKING:` and quote the annotation — the cycle
is then a type-checker concern only, and `ty` resolves it.

- [x] **Step 4: Write the llm half**

```python
# telegrind/llm.py
PROMPT_VERSION = "2026-09-11.1"


class LLMError(RuntimeError):
    """The model did not answer in the shape we asked for."""


EXTRACT_SYSTEM = f"""\
Ты извлекаешь факты из личного дневника в Telegram. Тебе дают окно
сообщений подряд — читай их вместе, соседние сообщения часто продолжают
друг друга.

{EXTRACTION_RULES}
- Поле `when` — это фраза о времени ровно так, как она написана в
  сообщении («вчера вечером», «в понедельник», «15 октября»). Не считай
  даты сам: у каждого сообщения свой час, и арифметику делает код.
  Если сообщение не называет времени — пустая строка.
- `message` — номер сообщения из блока «Сообщения для разбора».
  Факт, собранный из нескольких сообщений, принадлежит ПОСЛЕДНЕМУ из них:
  там он стал полным.
- Из блока «Контекст» извлекать не надо. Он нужен только чтобы понять,
  о чём речь.
"""

EXTRACT_TOOL = {
    "name": "record_facts",
    "description": "Записать факты, извлечённые из окна сообщений.",
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "integer"},
                        "kind": {"type": "string"},
                        "when": {"type": "string"},
                        "fields": {"type": "object"},
                    },
                    "required": ["message", "kind", "fields"],
                },
            }
        },
        "required": ["facts"],
    },
}


async def use_tool(
    system: str, user: str, tool: dict[str, Any], *, model: str | None = None
) -> dict[str, Any]:
    """One forced tool call. Returns the tool input, already a dict.

    Forced rather than suggested: the caller needs a structure, and an
    unforced call is free to answer in prose, which is a parse error
    dressed up as a success.
    """
    response = await client().messages.create(
        model=model or current_model(),
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise LLMError(f"{tool['name']} was not called")


async def say(system: str, user: str, *, model: str | None = None) -> str:
    """A plain prose answer."""
    response = await client().messages.create(
        model=model or current_model(),
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
```

- [x] **Step 5: Write `run`**

```python
# telegrind/extract.py
@dataclass(frozen=True, slots=True)
class Report:
    """What one pass did, in the shape /q reports it."""

    pending: int
    extracted: int
    facts: int
    failed: int
    complaints: int


async def run(
    session: AsyncSession,
    chat: Chat,
    cfg: ChatConfig,
    *,
    limit: int = 200,
    context_size: int = 10,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> Report:
    """One extraction pass over the unextracted tail.

    The caller owns the transaction. Nothing here commits: /q wants the
    facts and the marks to land together or not at all.
    """
    tail = await store.unextracted_tail(session, chat.id, limit=limit)
    if not tail:
        return Report(pending=0, extracted=0, facts=0, failed=0, complaints=0)

    context = await store.context_before(session, chat.id, tail[0], limit=context_size)
    vocabulary = taxonomy.render(await taxonomy.observed(session, chat.id))
    prompt = build_prompt(tail, context, vocabulary, cfg, chat.chat_id)
    model = llm.current_model()

    try:
        payload = await call(llm.EXTRACT_SYSTEM, prompt, llm.EXTRACT_TOOL, model=model)
    except Exception as exc:
        # Not marked: the tail stays pending and the next /q retries it.
        store.mark_failed(tail, f"{type(exc).__name__}: {exc}")
        log.warning("extraction pass failed for chat %s: %s", chat.chat_id, exc)
        return Report(
            pending=len(tail), extracted=0, facts=0, failed=len(tail), complaints=0
        )

    drafts, complaints = drafts_from(payload, tail, cfg)
    for complaint in complaints:
        log.warning("extraction complaint in chat %s: %s", chat.chat_id, complaint)

    now = datetime.now(UTC)
    written = 0
    for row in tail:
        written += await store.replace_facts(
            session,
            chat_pk=chat.id,
            message_pk=row.id,
            drafts=[d for d in drafts if d.message is row],
            model=model,
            prompt_version=llm.PROMPT_VERSION,
            now=now,
        )
    store.mark_extracted(
        tail, model=model, prompt_version=llm.PROMPT_VERSION, at=now
    )

    return Report(
        pending=len(tail),
        extracted=len(tail),
        facts=written,
        failed=0,
        complaints=len(complaints),
    )
```

- [x] **Step 6: Run the tests**

Run: `uv run pytest -v`
Expected: PASS

- [x] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/store.py telegrind/llm.py telegrind/extract.py tests/
git commit -m "feat: one extraction pass over the tail, marked and reportable"
```

---

### Task 6: The query spec and its SQL

Deterministic arithmetic. No model in this task at all.

**Files:**
- Create: `telegrind/query.py`
- Test: `tests/test_query.py`

**Interfaces:**
- Produces: `AGGREGATES`; `Unanswerable`; `Spec`; `Spec.parse(payload: dict, cfg: ChatConfig) -> Spec`;
  `Row(group: str | None, value: float | None, n: int)`;
  `Answer(rows: list[Row], skipped: int)`;
  `async run(session, chat_pk: int, spec: Spec) -> Answer`.

**Settled here:**

- The aggregate set is closed: `sum`, `count`, `avg`, `min`, `max`, `last`,
  `balance_by`. A question the spec cannot express is answered
  «не понял, переформулируй», never with a wrong number. Text-to-SQL is out of
  scope by the spec's own non-goals.
- The spec approach is not chosen out of injection fear. It is chosen because
  the aggregate is not always `sum`: weight is a series (`last`, `min`, `max`),
  habits are counts, debts are a running sum per counterparty.
- **`since`/`until` arrive as ISO dates and `until` is exclusive.** Half-open,
  so «за август» is `2026-08-01 .. 2026-09-01` and no fact lands in two months.
  Month boundaries are the reason the model computes these rather than
  `dateparser`: «август» through a relative parser gives *this day* in August,
  not the month.
- **`jsonb_typeof(fields->'x') = 'number'` is the numeric guard**, and it is
  exact rather than heuristic because `coerce.to_json_value` already made every
  parseable value a real JSON number. Rows that fail it are counted as
  `skipped` and reported, not silently left out of the total.

- [x] **Step 1: Write the failing test**

```python
# tests/test_query.py
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegrind.config import ChatConfig
from telegrind.query import Answer, Row, Spec, Unanswerable, run

CFG = ChatConfig(tz_offset=6, currency="KZT")


class FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.statements: list[object] = []

    async def execute(self, statement: object):
        self.statements.append(statement)
        rows = self.rows
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)


def test_parse_builds_a_half_open_period():
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


def test_an_unknown_aggregate_is_unanswerable():
    with pytest.raises(Unanswerable):
        Spec.parse({"kinds": ["expense"], "aggregate": "median"}, CFG)


def test_a_numeric_aggregate_without_a_field_is_unanswerable():
    with pytest.raises(Unanswerable):
        Spec.parse({"kinds": ["expense"], "aggregate": "sum"}, CFG)


def test_count_needs_no_field():
    assert Spec.parse({"kinds": ["habit"], "aggregate": "count"}, CFG).field is None


def test_balance_by_needs_something_to_group_on():
    with pytest.raises(Unanswerable):
        Spec.parse(
            {"kinds": ["loan"], "aggregate": "balance_by", "field": "amount"}, CFG
        )


async def test_run_guards_the_cast_and_reports_what_it_skipped():
    session = FakeSession([(None, 12300.0, 4, 1)])
    spec = Spec.parse(
        {"kinds": ["expense"], "aggregate": "sum", "field": "amount"}, CFG
    )

    answer = await run(session, chat_pk=1, spec=spec)

    assert answer == Answer(rows=[Row(group=None, value=12300.0, n=4)], skipped=1)
    rendered = str(session.statements[-1])
    assert "jsonb_typeof" in rendered
    assert "deleted_at IS NULL" in rendered


async def test_run_groups_when_asked():
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
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_query.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrind.query'`

- [x] **Step 3: Write the implementation**

```python
# telegrind/query.py
"""A question becomes a constrained spec, and the spec becomes SQL.

The model never does arithmetic. It says what to count and over what;
Postgres does the counting. A question this spec cannot express is
answered honestly rather than approximately — text-to-SQL as an escape
hatch is a non-goal.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, cast, func, null, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.config import ChatConfig
from telegrind.models import Fact

#: Closed on purpose. Expenses sum; weight is a series; habits are counts;
#: debts are a running sum per counterparty.
AGGREGATES = ("sum", "count", "avg", "min", "max", "last", "balance_by")

#: Everything but `count` reads a number out of JSONB.
NEEDS_FIELD = ("sum", "avg", "min", "max", "last", "balance_by")


class Unanswerable(Exception):
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
    where = [Fact.chat_pk == chat_pk, Fact.deleted_at.is_(None)]
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


async def _last(session: AsyncSession, where: list, spec: Spec) -> Answer:
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
```

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_query.py -v`
Expected: PASS, 7 tests

- [x] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/query.py tests/test_query.py
git commit -m "feat: a closed query spec, and SQL that cannot trip on a cast"
```

---

### Task 7: Question in, prose out

The two model calls that bracket the SQL.

**Files:**
- Create: `telegrind/answer.py`
- Modify: `telegrind/llm.py`
- Test: `tests/test_answer.py`

**Interfaces:**
- Produces in `llm`: `QUERY_SYSTEM`, `QUERY_TOOL`, `ANSWER_SYSTEM`.
- Produces in `answer`:
  `async spec_for(question: str, vocabulary: str, cfg: ChatConfig, today: date, *, call=llm.use_tool) -> Spec`
  and
  `async render(question: str, spec: Spec, result: query.Answer, cfg: ChatConfig, *, say=llm.say) -> str`.
- `spec_for` raises `query.Unanswerable`, which the handler turns into
  «не понял, переформулируй».

- [x] **Step 1: Write the failing test**

```python
# tests/test_answer.py
from datetime import date

import pytest

from telegrind.answer import render, spec_for
from telegrind.config import ChatConfig
from telegrind.query import Answer, Row, Spec, Unanswerable

CFG = ChatConfig(tz_offset=6, currency="KZT")


async def test_spec_for_passes_the_vocabulary_and_today_to_the_model():
    seen = {}

    async def call(system, user, tool, *, model=None):
        seen["system"] = system
        seen["user"] = user
        return {"kinds": ["expense"], "aggregate": "sum", "field": "amount"}

    spec = await spec_for(
        "сколько я потратил", "- expense (5): amount", CFG, date(2026, 9, 11), call=call
    )

    assert spec == Spec(kinds=("expense",), aggregate="sum", field="amount")
    assert "2026-09-11" in seen["system"]
    assert "- expense (5): amount" in seen["user"]


async def test_a_question_the_spec_cannot_express_raises():
    async def call(system, user, tool, *, model=None):
        return {"aggregate": "median", "field": "amount"}

    with pytest.raises(Unanswerable):
        await spec_for("медиана", "", CFG, date(2026, 9, 11), call=call)


async def test_render_hands_the_model_the_numbers_it_must_not_recompute():
    seen = {}

    async def say(system, user, *, model=None):
        seen["user"] = user
        return "За август 12 300 KZT."

    text = await render(
        "сколько я потратил в августе",
        Spec(kinds=("expense",), aggregate="sum", field="amount"),
        Answer(rows=[Row(group=None, value=12300.0, n=4)], skipped=0),
        CFG,
        say=say,
    )

    assert text == "За август 12 300 KZT."
    assert "12300" in seen["user"]
    assert "KZT" in seen["user"]


async def test_render_tells_the_model_about_the_rows_it_could_not_add_up():
    async def say(system, user, *, model=None):
        assert "3" in user
        return "ok"

    await render(
        "сколько",
        Spec(kinds=("expense",), aggregate="sum", field="amount"),
        Answer(rows=[Row(group=None, value=100.0, n=1)], skipped=3),
        CFG,
        say=say,
    )


async def test_render_says_plainly_when_there_is_nothing():
    async def say(system, user, *, model=None):
        return "unused"

    text = await render(
        "сколько",
        Spec(kinds=("expense",), aggregate="sum", field="amount"),
        Answer(rows=[], skipped=0),
        CFG,
        say=say,
    )

    assert "нет" in text.lower()
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_answer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegrind.answer'`

- [x] **Step 3: Add the prompts to `llm.py`**

```python
QUERY_SYSTEM_TEMPLATE = """\
Ты превращаешь вопрос о личном дневнике в структуру запроса. Ты НЕ
считаешь — считает база.

Сегодня {today}. Периоды задавай ISO-датами, `until` не включается:
«за август» это since=2026-08-01, until=2026-09-01.

Агрегаты: sum, count, avg, min, max, last, balance_by.
- sum — расходы и всё, что складывается.
- count — привычки и события: сколько раз.
- last / min / max — ряд измерений: последний вес, минимальный, максимальный.
- balance_by — сальдо по каждому контрагенту; group_by обязателен.
Если вопрос не ложится ни на один агрегат — верни aggregate «unknown».
Лучше честное «не понял», чем неправильное число.
"""

QUERY_TOOL = {
    "name": "build_query",
    "description": "Описать, что посчитать и по каким фактам.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kinds": {"type": "array", "items": {"type": "string"}},
            "aggregate": {"type": "string"},
            "field": {"type": "string"},
            "since": {"type": "string"},
            "until": {"type": "string"},
            "group_by": {"type": "string"},
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["field", "value"],
                },
            },
        },
        "required": ["aggregate"],
    },
}

ANSWER_SYSTEM = """\
Ты отвечаешь на вопрос по уже посчитанным числам. Числа даны — не меняй
их и не считай новых. Отвечай коротко, по-русски, одной-двумя фразами.
Если часть записей не попала в сумму, скажи об этом одной фразой.
"""
```

- [x] **Step 4: Write `answer.py`**

```python
# telegrind/answer.py
"""The two model calls that bracket the arithmetic.

The model says *what* to count and later reads the result out loud. It
never adds anything up: between these two calls sits Postgres.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import date

from telegrind import llm, query
from telegrind.config import ChatConfig
from telegrind.query import Spec


async def spec_for(
    question: str,
    vocabulary: str,
    cfg: ChatConfig,
    today: date,
    *,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> Spec:
    """Question → spec. Raises Unanswerable rather than guessing."""
    system = llm.QUERY_SYSTEM_TEMPLATE.format(today=today.isoformat())
    user = f"# Словарь этого чата\n{vocabulary}\n\n# Вопрос\n{question}"
    payload = await call(system, user, llm.QUERY_TOOL)
    return Spec.parse(payload, cfg)


async def render(
    question: str,
    spec: Spec,
    result: query.Answer,
    cfg: ChatConfig,
    *,
    say: Callable[..., Awaitable[str]] = llm.say,
) -> str:
    """Numbers → prose. Short-circuits on an empty result.

    An empty result needs no model: there is nothing to phrase, and a
    model asked to phrase nothing invents a reason.
    """
    if not result.rows or all(row.value is None and row.n == 0 for row in result.rows):
        return "По этому вопросу записей нет."

    payload = {
        "question": question,
        "aggregate": spec.aggregate,
        "field": spec.field,
        "currency": cfg.currency,
        "rows": [
            {"group": row.group, "value": row.value, "n": row.n}
            for row in result.rows
        ],
        "skipped": result.skipped,
    }
    return await say(
        llm.ANSWER_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str)
    )
```

- [x] **Step 5: Run the tests**

Run: `uv run pytest tests/test_answer.py -v`
Expected: PASS, 5 tests

- [x] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/answer.py telegrind/llm.py tests/test_answer.py
git commit -m "feat: a question becomes a spec, and numbers become prose"
```

---

### Task 8: `/q`

The trigger. Everything above becomes reachable here.

**Files:**
- Create: `telegrind/bot/handlers/query.py`
- Modify: `telegrind/bot/handlers/__init__.py`
- Test: `tests/test_qhandler.py`

**Interfaces:**
- Consumes: `store.upsert_message`, `handlers.acknowledge`, `handlers.RECEIPT_EMOJI`,
  `extract.run`, `taxonomy`, `answer.spec_for`, `answer.render`, `query.run`.
- Produces: `async ask(message, chat, config, session, bot) -> None`.

**Settled here:**

- **Import order is the whole risk, and `__init__.py` alone does not fix it.**
  `handlers.py` ends in `COMMAND_LIKE`, which swallows every slash message, so
  `handlers/__init__.py` must import `query` *first*. But `query` needs
  `RECEIPT_EMOJI` and `acknowledge`, and importing them **from `handlers.py`
  runs that module to completion first** — registering `record_command` ahead
  of `ask` and losing the race anyway. So the decorator-free half moves to
  `telegrind/bot/handlers/receipts.py` (`RECEIPT_CYCLE`, `RECEIPT_EMOJI`,
  `next_receipt`, `acknowledge`), re-exported from `handlers.py` so existing
  imports keep working. That removes the hazard rather than one instance of
  it. The Phase 1 plan named the wrong file for exactly this class of bug.
- **`/q` is stored like any message,** with `extractable=False` and the usual
  receipt. Nothing written is ever lost, and a question must never coin a kind.
- **The pre-reply is skipped on an empty tail.** «разбираю 0 сообщений…» is
  noise, and the empty case is the common one once the chat is caught up.
- Backlog latency is accepted: the first `/q` after a quiet week pays for the
  week. No background flush in this version.

- [x] **Step 1: Write the failing test**

```python
# tests/test_qhandler.py
from types import SimpleNamespace

from telegrind.bot.handlers import query as handler
from telegrind.config import ChatConfig
from telegrind.query import Answer, Row, Spec

CFG = ChatConfig(tz_offset=6, currency="KZT")
CHAT = SimpleNamespace(id=1, chat_id=7)
SPEC = Spec(kinds=("expense",), aggregate="sum", field="amount")


def report(**kwargs) -> SimpleNamespace:
    base = dict(pending=0, extracted=0, facts=0, failed=0, complaints=0)
    return SimpleNamespace(**(base | kwargs))


async def _unreachable(*args, **kwargs):
    raise AssertionError("should not have been called")


async def _vocabulary(session, chat_pk):
    return "- expense (5): amount"


async def _spec_for(question, words, cfg, today):
    return SPEC


async def _query_run(session, chat_pk, spec):
    return Answer(rows=[Row(group=None, value=100.0, n=1)], skipped=0)


async def _render(question, spec, result, cfg):
    return "Сто тенге."


def test_question_of_strips_the_command():
    assert handler.question_of("/q сколько я потратил") == "сколько я потратил"
    assert handler.question_of("/q@telegrind_bot сколько") == "сколько"
    assert handler.question_of("/q") == ""
    assert handler.question_of(None) == ""


async def test_an_empty_question_asks_for_one_and_runs_no_pass():
    text = await handler.answer_for(
        question="",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=_unreachable,
        spec_for=_unreachable,
        query_run=_unreachable,
        render=_unreachable,
        vocabulary=_unreachable,
    )

    assert "спроси" in text.lower()


async def test_the_answer_is_the_rendered_prose():
    async def passes(session, chat, cfg):
        return report(pending=2, extracted=2, facts=2)

    text = await handler.answer_for(
        question="сколько",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=passes,
        spec_for=_spec_for,
        query_run=_query_run,
        render=_render,
        vocabulary=_vocabulary,
    )

    assert text == "Сто тенге."


async def test_a_pass_that_failed_is_reported_alongside_the_answer():
    async def passes(session, chat, cfg):
        return report(pending=3, failed=3)

    text = await handler.answer_for(
        question="сколько",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=passes,
        spec_for=_spec_for,
        query_run=_query_run,
        render=_render,
        vocabulary=_vocabulary,
    )

    assert text.startswith("Сто тенге.")
    assert "3 сообщени" in text


async def test_a_question_the_spec_cannot_express_is_refused_honestly():
    from telegrind.query import Unanswerable

    async def passes(session, chat, cfg):
        return report()

    async def refuses(question, words, cfg, today):
        raise Unanswerable("median")

    text = await handler.answer_for(
        question="медиана",
        chat=CHAT,
        config=CFG,
        session=object(),
        passes=passes,
        spec_for=refuses,
        query_run=_unreachable,
        render=_unreachable,
        vocabulary=_vocabulary,
    )

    assert "переформулируй" in text
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_qhandler.py -v`
Expected: FAIL with `ImportError: cannot import name 'query'`

- [x] **Step 3: Write the handler**

```python
# telegrind/bot/handlers/query.py
"""/q — the only reason extraction ever has to have happened.

Asking is the trigger. Deferred parsing and the dialogue product are one
mechanism, not two features that coexist, so there is no second trigger
here and no background flush.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import answer as answers
from telegrind import extract, query, store, taxonomy
from telegrind.bot.handlers.handlers import RECEIPT_EMOJI, acknowledge
from telegrind.bot.router import router
from telegrind.config import ChatConfig
from telegrind.models import Chat

log = logging.getLogger(__name__)


def question_of(text: str | None) -> str:
    """Everything after /q, with an @mention suffix tolerated."""
    if not text:
        return ""
    _, _, rest = text.partition(" ")
    return rest.strip()


async def _vocabulary(session: AsyncSession, chat_pk: int) -> str:
    return taxonomy.render(await taxonomy.observed(session, chat_pk))


async def answer_for(
    question: str,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    *,
    passes: Callable[..., Awaitable[extract.Report]] = extract.run,
    spec_for: Callable[..., Awaitable[query.Spec]] = answers.spec_for,
    query_run: Callable[..., Awaitable[query.Answer]] = query.run,
    render: Callable[..., Awaitable[str]] = answers.render,
    vocabulary: Callable[..., Awaitable[str]] = _vocabulary,
) -> str:
    """The whole answer as one string. No Telegram in here, so it tests."""
    if not question:
        return "Спроси что-нибудь после /q."

    report = await passes(session, chat, config)

    words = await vocabulary(session, chat.id)
    today = datetime.now(tz=config.tz).date()
    try:
        spec = await spec_for(question, words, config, today)
    except query.Unanswerable as exc:
        log.info("unanswerable question in chat %s: %s", chat.chat_id, exc)
        return "Не понял вопрос, переформулируй."

    result = await query_run(session, chat.id, spec)
    text = await render(question, spec, result, config)

    if report.failed:
        text += f"\n\n({report.failed} сообщений не удалось разобрать.)"
    if report.complaints:
        text += f"\n({report.complaints} фактов не удалось привязать.)"
    return text


@router.message(Command("q"))
async def ask(
    message: Message,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Store the question, catch the log up, then answer it."""
    async with session.begin():
        row, created = await store.upsert_message(
            session, chat, message, extractable=False
        )
        row.receipt_emoji = RECEIPT_EMOJI
    await acknowledge(bot, message.chat.id, message.message_id, RECEIPT_EMOJI)

    # Said in a transaction of its own, and outside the answering one: the
    # first /q after a quiet week pays for the week, and holding a write
    # transaction open across two model calls to announce that is the wrong
    # shape even at one user. A bare read here would autobegin and make the
    # `session.begin()` below raise «a transaction is already begun» —
    # measured against SQLAlchemy 2 on 2026-09-11.
    async with session.begin():
        pending = len(await store.unextracted_tail(session, chat.id))
    if pending:
        await bot.send_message(message.chat.id, f"Разбираю {pending} сообщений…")

    async with session.begin():
        text = await answer_for(question_of(message.text), chat, config, session)
    await bot.send_message(message.chat.id, text)
```

- [x] **Step 4: Register it ahead of the catch-all**

```python
# telegrind/bot/handlers/__init__.py
# Importing a handler module is what registers it, and registering an
# observer is also what subscribes its update type — aiogram derives
# allowed_updates from the handlers that exist.
#
# Order matters only *within* an observer. `handlers` ends in a catch-all
# message handler AND in a slash catch-all ahead of it, so `query` must
# come first or /q is swallowed and stored as an ordinary command.
# `reactions` is a different observer and does not compete.
from . import query as query
from . import handlers as handlers
from . import reactions as reactions
```

Ruff's isort rule will want these alphabetised. Add `# noqa: I001` on the
block — and check that it is *needed* before leaving it in, because an unused
noqa is itself an RUF100 error. (This bit twice in Phase 1.)

- [x] **Step 5: Run the tests**

Run: `uv run pytest -v`
Expected: PASS

- [x] **Step 6: Verify the registration order is real, not assumed**

```bash
uv run python -c "
from telegrind.bot.setup import setup_dispatcher
dp = setup_dispatcher()
for r in dp.sub_routers:
    print([h.callback.__name__ for h in r.message.handlers])
print(sorted(dp.resolve_used_update_types()))
"
```
Expected: `ask` appears **before** `record_command`, and the update types
include `message`, `edited_message` and `message_reaction`. Read the handlers
off the **sub-router**, not off `dp.message` — everything here is registered
on `telegrind.bot.router.router`, so `dp.message.handlers` is empty and prints
a reassuring `[]` that proves nothing.

- [x] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/bot/handlers/ tests/test_qhandler.py
git commit -m "feat: /q catches the log up and answers from the fact table"
```

---

### Task 9: An edit re-extracts that one message

The spec's second trigger. Phase 1's `record_edited` only clears
`extracted_at`; that is right for a message nobody has parsed yet and wrong
for one whose facts are now stale.

**Files:**
- Modify: `telegrind/extract.py`, `telegrind/bot/handlers/handlers.py`
- Test: `tests/test_extract.py`, `tests/test_ingest.py`

**Interfaces:**
- Produces: `async run_for(session, chat, cfg, row: LoggedMessage, *, context_size=10, call=llm.use_tool) -> Report`.

**Settled here:**

- **The single message goes through the same window builder**, with its
  neighbours as read-only context. A message re-extracted in isolation can coin
  a different `kind` than it would in company — which is the exact failure
  batching exists to prevent.
- **An edit of an *unextracted* message stays a plain overwrite.** No LLM call:
  there is nothing stale to fix, and the next `/q` will read it anyway.
- **The flag is derived from the edited text, never hardcoded.** Deriving beats
  carrying the previous value forward, because it also tracks a user editing a
  command into prose or prose into a command.
- The re-extraction shares `replace_facts`, so a fact that disappeared from the
  edited text is tombstoned and one that changed is updated in place.

- [x] **Step 1: Refactor `run` around a shared `_pass`**

```python
# telegrind/extract.py
async def _pass(
    session: AsyncSession,
    chat: Chat,
    cfg: ChatConfig,
    tail: list[LoggedMessage],
    *,
    context_size: int,
    call: Callable[..., Awaitable[dict]],
) -> Report:
    """Everything `run` did once the tail was chosen."""
    # Move the body of Task 5's `run` here verbatim, starting at the
    # `context = await store.context_before(...)` line. Nothing in it
    # changes; only where the tail comes from does.


async def run(session, chat, cfg, *, limit=200, context_size=10, call=llm.use_tool):
    tail = await store.unextracted_tail(session, chat.id, limit=limit)
    if not tail:
        return Report(pending=0, extracted=0, facts=0, failed=0, complaints=0)
    return await _pass(session, chat, cfg, tail, context_size=context_size, call=call)


async def run_for(
    session: AsyncSession,
    chat: Chat,
    cfg: ChatConfig,
    row: LoggedMessage,
    *,
    context_size: int = 10,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> Report:
    """Re-extract one edited message, in the company of its neighbours.

    The same window builder, a tail of one. Isolation is what makes a
    re-extraction coin a synonym for a kind it already had.
    """
    return await _pass(session, chat, cfg, [row], context_size=context_size, call=call)
```

- [x] **Step 2: Write the failing handler test**

Append to `tests/test_ingest.py`:

```python
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace

from telegrind import extract
from telegrind.bot.handlers import handlers
from telegrind.config import ChatConfig
from telegrind.models import KIND_TEXT, Chat, LoggedMessage

CFG = ChatConfig(tz_offset=6, currency="KZT")


class EditSession:
    """Enough session for record_edited: one lookup, reused by the upsert."""

    def __init__(self, existing: LoggedMessage | None) -> None:
        self.existing = existing

    @contextlib.contextmanager
    def _txn(self):
        yield

    def begin(self):
        @contextlib.asynccontextmanager
        async def ctx():
            yield

        return ctx()

    async def execute(self, statement: object):
        row = self.existing
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def add(self, obj: object) -> None:
        pass

    async def flush(self) -> None:
        pass


class Recorder:
    async def set_message_reaction(self, **kwargs) -> None:
        pass


def stored(*, extracted: bool) -> LoggedMessage:
    return LoggedMessage(
        id=42,
        chat_pk=1,
        message_id=10,
        kind=KIND_TEXT,
        text="4500 такси",
        tg_date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        raw={},
        receipt_emoji=handlers.RECEIPT_EMOJI,
        extracted_at=datetime(2026, 9, 11, 4, tzinfo=UTC) if extracted else None,
    )


def edit() -> SimpleNamespace:
    return SimpleNamespace(
        message_id=10,
        chat=SimpleNamespace(id=7),
        text="5500 такси",
        caption=None,
        voice=None,
        forward_origin=None,
        date=datetime(2026, 9, 11, 3, tzinfo=UTC),
        edit_date=1789094740,
        model_dump=lambda mode="json": {},
    )


async def test_an_edit_of_an_extracted_message_re_extracts_it(monkeypatch):
    called: list[int] = []

    async def fake_run_for(session, chat, cfg, row, **kwargs):
        called.append(row.id)
        return SimpleNamespace(facts=1, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=True)),
        CFG,
        Recorder(),
    )

    assert called == [42]


async def test_an_edit_of_an_unextracted_message_makes_no_call(monkeypatch):
    called: list[int] = []

    async def fake_run_for(session, chat, cfg, row, **kwargs):
        called.append(row.id)
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    await handlers.record_edited(
        edit(),
        Chat(id=1, chat_id=7),
        EditSession(stored(extracted=False)),
        CFG,
        Recorder(),
    )

    assert called == []


async def test_editing_a_command_leaves_it_out_of_the_extractor(monkeypatch):
    called: list[int] = []

    async def fake_run_for(session, chat, cfg, row, **kwargs):
        called.append(row.id)
        return SimpleNamespace(facts=0, failed=0)

    monkeypatch.setattr(extract, "run_for", fake_run_for)

    existing = stored(extracted=False)
    existing.text = "/q сколкьо я потратил"
    existing.extractable = False
    message = edit()
    message.text = "/q сколько я потратил"

    await handlers.record_edited(
        message, Chat(id=1, chat_id=7), EditSession(existing), CFG, Recorder()
    )

    assert existing.extractable is False
    assert called == []
```

`monkeypatch.setattr(extract, "run_for", …)` patches the module attribute, so
`handlers.py` must call `extract.run_for(...)` — not `from … import run_for`,
which would bind the real function at import time and make the patch useless.

- [x] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_ingest.py -v -k edit`
Expected: FAIL — `record_edited` makes no call today.

- [x] **Step 4: Change `record_edited`**

```python
@router.edited_message()
async def record_edited(
    edited_message: Message,
    chat: Chat,
    session: AsyncSession,
    config: ChatConfig,
    bot: Bot,
) -> None:
    """Overwrite the text, and re-extract if there was anything to redo.

    The check has to happen *before* upsert_message, which clears
    extracted_at by design: after it, «was this already parsed» has no
    answer left.
    """
    async with session.begin():
        previous = await store.get_message(session, chat.id, edited_message.message_id)
        was_extracted = previous is not None and previous.extracted_at is not None

        # Derived, not hardcoded: this observer has no COMMAND_LIKE ahead of
        # it, and upsert_message assigns the flag unconditionally — so `True`
        # here would turn a stored /q back into extractor input the first
        # time the user fixes a typo in their own question.
        parses = not (edited_message.text or "").startswith("/")
        row, created = await store.upsert_message(
            session, chat, edited_message, extractable=parses
        )
        emoji = RECEIPT_EMOJI if created else next_receipt(row.receipt_emoji)
        row.receipt_emoji = emoji

        if was_extracted and parses:
            report = await extract.run_for(session, chat, config, row)
            log.info("re-extracted message %s: %s fact(s)", row.id, report.facts)

    await acknowledge(bot, edited_message.chat.id, edited_message.message_id, emoji)
```

`config: ChatConfig` is a new kwarg on this handler; the middleware already
injects it, so nothing else changes.

- [x] **Step 5: Run the tests**

Run: `uv run pytest -v`
Expected: PASS

- [x] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
git add telegrind/extract.py telegrind/bot/handlers/handlers.py tests/
git commit -m "feat: editing an extracted message redoes it, with its neighbours"
```

---

### Task 10: The quality gate, the docs, and the walkthrough

The unit tests prove the plumbing. Nothing so far proves the *prompt* works,
and that is the part that cannot be reasoned about — only measured.

**Files:**
- Modify: `tests/fixtures/extraction.yaml`
- Create: `tests/test_extraction_quality.py`
- Modify: `CLAUDE.md`, `README.md`, `.env.dist`
- Modify: `docs/superpowers/plans/2026-09-11-dialogue-first-phase-2.md` (the outcome report)

**Why the fixture needs work first.** It is a 97-line corpus of real messages,
deliberately kept through Phase 1 — but its labels are registry-era. Two
problems, both fixable:

- `category:` names a closed set (`expense`, `loan`, `telemetry`, `wish`,
  `facts`) that no longer exists. `kind` is free-form now, so **equality on it
  cannot be asserted** and the label is dropped.
- `date:` is the durable half and is still exact — but it was computed against
  an implied "now" that no longer exists. `«41 бат массаж вчера вечером» →
  2026-09-08` only means anything if the message was *sent* on 2026-09-09. So
  each row with a `date:` gains a `sent:`, and the test builds a message with
  that `tg_date`.

- [x] **Step 1: Rewrite the fixture header and rows**

```yaml
# message -> what the extractor must get right about it.
#   sent:  the message's own tg_date (ISO, chat timezone). Required when
#          `date:` is given — «вчера» means nothing without it.
#   date:  the day `at` must resolve to.
# `kind` is deliberately NOT asserted: the taxonomy is free-form now, and
# the old `category:` labels named a registry that no longer exists.
# Run on demand: `uv run pytest -m llm`. Costs tokens; never in the default suite.
- message: "4500 такси"
- message: "41 бат массаж вчера вечером"
  sent: "2026-09-09 21:00"
  date: "2026-09-08"
- message: "потратил тысячу на продукты в понедельник"
  sent: "2026-09-09 21:00"
  date: "2026-09-07"
# ...the rest of the corpus, `category:` removed throughout
```

- [x] **Step 2: Write the gate**

```python
# tests/test_extraction_quality.py
"""Does the prompt actually work? Costs tokens; `-m llm` only."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from telegrind.config import ChatConfig
from telegrind.extract import build_prompt, drafts_from
from telegrind.llm import EXTRACT_SYSTEM, EXTRACT_TOOL, use_tool
from telegrind.models import KIND_TEXT, LoggedMessage

pytestmark = pytest.mark.llm

CFG = ChatConfig(tz_offset=6, currency="KZT")
CASES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "extraction.yaml").read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["message"])
async def test_the_corpus_still_extracts(case: dict):
    sent = case.get("sent", "2026-09-11 12:00")
    row = LoggedMessage(
        id=1,
        chat_pk=1,
        message_id=1,
        kind=KIND_TEXT,
        text=case["message"],
        tg_date=datetime.fromisoformat(sent).replace(tzinfo=CFG.tz),
        raw={},
    )
    prompt = build_prompt([row], [], "(пусто)", CFG, chat_id=1)
    payload = await use_tool(EXTRACT_SYSTEM, prompt, EXTRACT_TOOL)

    drafts, complaints = drafts_from(payload, [row], CFG)

    assert complaints == []
    assert drafts, "the message states a fact and none came back"
    if "date" in case:
        assert CFG.localized(drafts[0].at).date().isoformat() == case["date"]
```

- [x] **Step 3: Run the gate against the real model**

Run: `uv run pytest -m llm -v`
Expected: PASS. If a row fails, **fix the prompt, not the fixture** — the
corpus is real messages and the labels are what the bot is supposed to do.
Record any row you deliberately relax, and why.

- [x] **Step 4: Fix the `.env.dist` comment**

`LLM_MODEL_ESCALATE` says "reserved for Phase 2 re-extraction" and Phase 2
does not use it. Change the comment to say it is not wired up yet, or delete
the variable — do not leave it describing a feature that does not exist.

- [x] **Step 5: Update the docs**

- `CLAUDE.md`: the `## What This Is` paragraph loses "a later batch pass
  derives facts" as future tense; `## Architecture` gains the `/q` flow and
  `taxonomy.py`, `extract.py`, `query.py`, `answer.py` under **Key layers**;
  the Phase line says Phase 2 is done and Phase 3 is not.
- `README.md`: move "Batch extraction over a window" and "`/q`" from **TODO**
  to **DONE**, and say in **How it works** that asking is what triggers the
  parse.

- [ ] **Step 6: Walk it end to end on the dev stack**

```bash
docker compose up -d --build
docker compose logs -f bot
```

In the chat, in order — and check the database after each:

1. Write three messages of different kinds, one of them a continuation
   (`хлеб 500` then `и молоко 300`). Confirm no reply and three 💔.
2. `/q сколько я потратил сегодня` → expect «Разбираю 3 сообщений…» then an
   answer. Confirm in Postgres that the continuation's facts hang off the
   **second** message, not the first.
3. `/q` again → no «разбираю» line this time; the tail is empty.
4. Edit the first message's amount → the heart advances **and** its facts are
   redone. Confirm `fact.updated_at` moved and the old value is gone.
5. Tap the heart → `tombstoned N fact(s)`; ask the same question again and the
   number must drop. Remove the reaction → `restored N fact(s)` and the number
   comes back. **The restore half was never verified in Phase 1 — verify it
   here.**
6. Send a photo with no caption. Confirm it is stored, has no 💔 problem, and
   stays `extracted_at IS NULL` with `extract_error IS NULL` after a `/q`.
7. Ask something the spec cannot express («что я чувствовал в августе») and
   confirm the honest refusal rather than a number.

```sql
select m.message_id, m.extractable, m.extracted_at is not null as done,
       m.extract_error, f.seq, f.kind, f.at, f.fields, f.deleted_at
from message m left join fact f on f.message_pk = m.id
order by m.tg_date desc limit 40;
```

- [ ] **Step 7: Stop the dev bot**

```bash
docker compose stop bot
```
Housekeeping, not safety. An earlier draft of this step claimed the dev stack
polls the production `BOT_TOKEN`; it does not. Verified 2026-09-11 via `getMe`:
`.env` here is `@assinstantbot`, prod is `@telegrindbot`, and prod runs on the
homeserver, not on this box — the only telegrind containers here are the
`telegrind-dev` ones. There is no two-poller collision to avoid.

- [ ] **Step 8: Write the outcome into this plan and commit**

Replace this step with what the walkthrough actually found — the bugs the unit
tests could not catch are the reason the step exists. Phase 1's manual gate
found two (an `edit_date` that is a Unix int, and a container database URL);
assume this one finds some too.

```bash
git add -A
git commit -m "docs: Phase 2 walked end to end on the dev stack"
```

---

## Open questions, none blocking

- **Window budget.** 200 messages per pass is the spec's number and is
  untested against real volume. A backlog larger than that answers from a
  partial parse and the next `/q` catches up further. Watch it on the first
  import in Phase 3.
- **Context size.** 10 neighbours is a guess. The thing it buys is
  `хлеб 500` / `и молоко 300` staying one trip; if that keeps failing, raise it
  before touching the prompt.
- **`kind` drift.** The observed taxonomy is the only brake. If the corpus
  starts showing `expense`/`расход`/`spending` as three kinds, the fix is
  probably a merge pass, not a stronger prompt — but do not build it before
  seeing it.

---

## Execution outcome — 2026-09-11, inline

Tasks 1–9 and Task 10 steps 1–5 are done, each on its own commit. The default
suite is 124 tests; the `-m llm` gate is 23 and passes 23/23 against
`claude-haiku-4-5`. `ruff check`, `ruff format` and `ty check` are clean at
every commit. Steps 6–8 below are the manual walkthrough and are not done.

**Five deviations from the plan as written, all found by running it:**

1. **`session.begin()` cannot be called twice.** Task 8's `ask` read the
   pending count outside a transaction and then opened one; SQLAlchemy 2
   autobegins on the read, so the second `begin()` raises *«A transaction is
   already begun on this Session»* (measured against a live `Session`). The
   count now runs in a transaction of its own. The plan text is corrected.
2. **`handlers/__init__.py` alone does not win the registration race.**
   `query.py` needs `RECEIPT_EMOJI` and `acknowledge`, and importing them from
   `handlers.py` runs that module to completion — registering the
   `COMMAND_LIKE` catch-all — before `/q`'s own decorator. The decorator-free
   half moved to `telegrind/bot/handlers/receipts.py`, re-exported from
   `handlers.py` so existing imports still work. Verified: the router's
   handlers are `['ask', 'record_command', 'record_voice', 'record_text']`.
   The plan's own verification command was also wrong — it printed
   `dp.message.handlers`, which is empty here because everything registers on
   the sub-router, so it returned a reassuring `[]` that proved nothing.
3. **`dateparser` returns None for «вчера вечером».** Not a wrong date — no
   date at all, for every Russian `<day> <time-of-day>` compound
   (`вчера утром`, `позавчера вечером`, `в понедельник утром`, …). The whole
   phrase then fell through to the message's own timestamp, so «вчера» quietly
   became today. This is the most common way he writes a date, and the
   `-m llm` gate is what caught it. `coerce.to_instant` now strips the
   time-of-day qualifier and re-parses; the day, which is what a fact is filed
   under, survives. `coerce.py` was named as untouched in the Global
   Constraints — that was about the numeric guard, which is unchanged.
4. **`tests/conftest.py` loads `.env` through python-dotenv.** `.env` has CRLF
   line endings; sourcing it in a shell carries the `\r` into
   `ANTHROPIC_API_KEY`, and the Anthropic client reports the resulting illegal
   header *by printing the whole key*. Loading it in-process avoids both the
   failure and the disclosure, and makes `uv run pytest -m llm` work as the
   plan says it does.
5. **Lint fixes the plan's code did not anticipate:** `ANN` wants a return
   annotation on every test and nested fake; `N818` wants an `Error` suffix on
   `Unanswerable` (kept, with a `noqa` — it reads as the refusal, not an
   error); `ty` rejects a bare `dict` for the Anthropic `tools` parameter, so
   `EXTRACT_TOOL` and `QUERY_TOOL` are typed `ToolParam`.
