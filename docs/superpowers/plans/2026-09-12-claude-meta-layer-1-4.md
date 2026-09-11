# The Claude meta layer, build-order steps 1–4 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every incoming message by a stored verdict — a fact gets 💔 and the extraction tail, a question gets an answer, anything else gets handed to a `claude -p` turn that replies in the chat — and store what the bot itself says, so the dialogue has two sides in one table.

**Architecture:** The message row is committed first and unconditionally; only then does one cheap model call decide `fact | question | talk`, and the verdict is written on the row where `extractable` used to be read. The receipt becomes the routing signal (💔 fact, 👀 handed to Claude, nothing yet = queued). A question goes to the existing `answer.spec_for` → SQL → prose path, and a refusal falls through to Claude instead of dying. Claude is an aiogram-side module inside the same process: it derives a session id from the message it is answering, queues one turn at a time per session, spawns `claude -p`, and sends the answer back as a reply.

**Tech Stack:** Python 3.14, aiogram 3.27 (Bot API 9.6), SQLAlchemy 2 (asyncpg), Alembic, anthropic 0.97, the `claude` CLI (2.1.269 on this box), pytest with `asyncio_mode = "auto"`, uv.

**Spec:** `docs/superpowers/specs/2026-09-11-claude-meta-layer-design.md`

---

## Global Constraints

- **Nothing written is ever lost, and the classifier must never be able to break
  that.** `store.upsert_message` runs and its transaction commits *before* any
  model call. A classifier failure writes `fact` explicitly and the behaviour
  degrades to exactly what ships today.
- **The verdict is never null.** Four values, and every row gets one:
  `fact`, `question`, `talk`, `system`. `system` is every message the bot or
  Claude sends, and every slash command that is not `/q`. Null would mean three
  different things at once.
- **The verdict is read; `extractable` is written but no longer read.** This
  plan does the *expand* half only: add `verdict`, backfill it, keep writing
  `extractable` in step with it, and move every read (`unextracted_tail`) onto
  `verdict`. Dropping the column is a later contract step taken by hand, per the
  spec's migration-reversibility rule. Do **not** drop it here.
- **One place decides the verdict.** `classify.verdict_for` is it. Two rules that
  can disagree — a `COMMAND_LIKE` filter *and* a classifier — is the bug the
  spec forbids, so the slash rule moves inside `classify` and the filter goes.
- **A message with nothing readable never reaches the model.** A sticker, a
  photo without a caption, a voice note: `classify.presumed` returns `fact`
  without a call. `store._has_content()` already keeps such a row out of the
  tail, so today's behaviour is preserved and no call is spent per sticker.
- **`reply_to_message_id` is deprecated at aiogram 3.27** — the field carries
  `json_schema_extra={"deprecated": True}` in the installed tree. Use
  `ReplyParameters(message_id=...)` on `send_message`. `docs/telegram-bot-api.md`
  does not record this; Task 10 adds the line.
- **Telegram does not nest replies.** `message.reply_to_message.reply_to_message`
  is always `None` — the API includes one level only. The second hop of the
  turn-key resolution therefore reads *our own* stored row for the parent, which
  is why storing outbound messages (Task 2) must land before the Claude handler
  (Tasks 7–9).
- **Step 4 is exercised with the bot on the host, not in the container.** The
  `telegrind-bot:dev` image has no `claude` binary, no subscription login, no
  `~/.claude`. The documented local path already works and is the dev runtime
  for this plan: `uv sync && uv run python main.py`, with `.env`'s host
  `DATABASE_URL` (`localhost:5433`) and `docker compose up postgres` for the
  database. No compose change, no credential mount, no new image layer.
- **The meta layer is a guest.** `telegrind/meta/` takes a config object and
  callables; it imports nothing from `telegrind.store`, `telegrind.query` or
  `telegrind.taxonomy`. Everything it needs is handed in. Separating it later
  should be deleting `telegrind/bot/routing.py`'s import, not unpicking a merge.
- **A bare read on the handler's session autobegins a transaction, and the next
  `session.begin()` then raises «a transaction is already begun».** The session
  injected by `populate_chat_data` arrives with no transaction open, and any
  `session.execute`/`session.get` outside `session.begin()` opens one that never
  closes. So **every read wraps in its own `session.begin()`**, including a read
  whose only purpose is to fetch a row for a later write. This has already been
  hit three times in this codebase — `query.py:89-93` (where the comment
  documents it), the edit path's `record_edited`, and `meta_wiring.speak` — and
  **the fake-session tests cannot see it**: a hand-written fake session has no
  transaction state, so the suite passes on code that raises against Postgres.
  Any task touching a session is reviewed by reading the code, not by running
  the tests.
- **Tests are pure unit tests: no live database, no network, no subprocess.**
  Follow the existing convention — `SimpleNamespace` fakes for aiogram objects,
  unattached SQLAlchemy model instances, hand-written fake sessions
  (`tests/test_store.py`, `tests/test_ingest.py`). Every model call and every
  process spawn goes behind a keyword parameter with a default, so a test passes
  a fake.
- Run tests with `uv run pytest`. Lint with `uv run ruff check` and
  `uv run ruff format`. Type-check with `uv run ty check`. All three clean at
  every commit.

## Scope, and two named deviations from the spec's build order

This plan covers build-order steps **1–4**. Steps 5–8 are out of scope; step 7
(delivery and self-modification) has a different risk profile and gets its own
plan.

| Spec step | Where it lands here |
| --- | --- |
| 1. Classifier, verdict column, `extractable` derived, re-classify on edit, receipt only on facts | Tasks 1, 3, 5, 6 |
| 2. The bot stores its own outbound messages | Task 2 |
| 3. Natural-language questions; a refusal becomes a hand-off | Tasks 4, 5, 9 |
| 4. The Claude handler on dev | Tasks 7, 8, 9 |

**Deviation 1 — steps 1 and 3 are folded, at the user's instruction.** Shipping
"receipt only on facts" before a reply path exists leaves a question with
neither a reaction nor an answer. Nothing is in production yet, so that
intermediate state is simply not built: Task 4 gives `query.py` an answerer with
a distinguishable refusal, and Task 5 is the first commit that changes what the
user sees — at which point all three arms already work.

**Deviation 2 — fork-per-turn moves from step 5 into step 4.** The spec puts
"forking per turn from the parent message" in step 5, but a derived session id
without forking does not survive three turns. Trace it: Claude answers `M1` in
session `uuid5(M1)` and sends `A1` with `reply_to = M1`. The user replies to
`A1` with `F1`; the two-hop rule resolves the turn to `M1`, so a resume-in-place
run continues session `uuid5(M1)` and answers with `A2`, sent with
`reply_to = F1`. The user replies to `A2` with `F2`; the two-hop rule now
resolves to `F1`, and `uuid5(F1)` does not exist, because resume-in-place never
created it. `--fork-session` is what makes the derived id self-consistent, and
it is one extra flag. **The warm base stays in step 5** — that is the separable
half (the cost saving and the rebuild command), and this plan pays a cold
session per new conversation. Measured on this box 2026-09-12: a cold turn cost
$0.21 against 20k cache-creation tokens, which is the spec's number and the
reason step 5 exists.

**Not built: the liveness check**, though the spec lists it in step 4. That
section was written for a draft in which Claude polled the `message` table from
outside the bot. "Claude is a handler, not a second process" was decided
2026-09-12 and postdates it: inbound and outbound are now the same process, so a
dead bot means no turn is ever spawned, not a turn that answers into the void.
Stated here rather than silently omitted, so it can be overruled in one line.

## File Structure

| File | Responsibility |
| --- | --- |
| Create `alembic/versions/<rev>_message_verdict.py` | Add `message.verdict`, backfill it from `extractable`, drop it on downgrade. |
| Create `telegrind/classify.py` | The four verdicts, the rules that need no model call, and the one call that decides the rest. |
| Create `telegrind/bot/outbound.py` | Send a message *and* store it. The only way the bot speaks. |
| Create `telegrind/bot/answering.py` | Question → numbers → prose, and a refusal that is a `None`. Registers nothing, so routing can import it. |
| Create `telegrind/bot/routing.py` | Verdict → receipt, answer, or hand-off. The one place the three arms meet. |
| Create `telegrind/meta/__init__.py` | The module's public surface: `setup`, `hand_over`. |
| Create `telegrind/meta/config.py` | `MetaConfig`: the admin allowlist, the binary, the cwd, the timeout. Read from env. |
| Create `telegrind/meta/sessions.py` | Session id derivation and the two-hop turn key. No I/O beyond one row lookup, handed in. |
| Create `telegrind/meta/runtime.py` | Spawning `claude -p`, the timeout, the exit code, the JSON. |
| Create `telegrind/meta/queue.py` | One turn at a time per session key; merge what is still queued. |
| Create `telegrind/bot/meta_wiring.py` | The four callables the meta layer needs from telegrind, and nothing more. Deleting this file is what separating the module later means. |
| Modify `telegrind/models.py` | The `verdict` column and its docstring. |
| Modify `telegrind/store.py` | `verdict` on `upsert_message`; `unextracted_tail` filters on it; `reply_to(row)`. |
| Modify `telegrind/llm.py` | `CLASSIFY_SYSTEM`, `CLASSIFY_TOOL`, `META_SYSTEM`. |
| Modify `telegrind/extract.py` | `author_of` names the bot, now that the bot's own messages are in the window. |
| Modify `telegrind/bot/handlers/handlers.py` | One ingest handler; the verdict decides everything after the commit. |
| Modify `telegrind/bot/handlers/query.py` | Reduced to the `/q` handler, which now sets the verdict and routes like any other question. |
| Modify `telegrind/bot/handlers/receipts.py` | `HANDED_OVER = "👀"` and `clear_receipt`. |
| Modify `telegrind/bot/setup.py` | Build the `MetaConfig` and wire the meta layer in. |
| Modify `.env.dist`, `docs/telegram-bot-api.md`, `CLAUDE.md`, `.claude/memory/project.md` | Task 10. |
| Create `tests/test_classify.py`, `tests/test_outbound.py`, `tests/test_routing.py`, `tests/test_meta_sessions.py`, `tests/test_meta_runtime.py`, `tests/test_meta_queue.py` | One per unit. |

---

### Task 1: The verdict column

The foundation. Nothing behaves differently after this task — the column exists,
it is backfilled, it is written, and `unextracted_tail` reads it instead of
`extractable`.

**Files:**
- Create: `alembic/versions/<generated>_message_verdict.py`
- Modify: `telegrind/models.py`, `telegrind/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `models.VERDICT_FACT = "fact"`, `VERDICT_QUESTION = "question"`,
  `VERDICT_TALK = "talk"`, `VERDICT_SYSTEM = "system"`, `VERDICTS` (a tuple of
  the four); `LoggedMessage.verdict: Mapped[str]`;
  `store.upsert_message(session, chat, msg, *, extractable=True, verdict=VERDICT_FACT)`;
  `store.reply_to(row: LoggedMessage) -> int | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py — append
from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, VERDICT_SYSTEM, VERDICTS


def test_the_four_verdicts_are_closed() -> None:
    """Null would mean «not classified», «classifier failed» and «not a fact»
    all at once, which is undebuggable exactly when it misroutes."""
    assert VERDICTS == ("fact", "question", "talk", "system")


def test_reply_to_reads_the_parent_id_off_the_row() -> None:
    row = LoggedMessage(raw={"reply_to_message": {"message_id": 77}})
    assert store.reply_to(row) == 77


def test_reply_to_is_none_for_a_plain_message() -> None:
    assert store.reply_to(LoggedMessage(raw={})) is None
    assert store.reply_to(LoggedMessage(raw=None)) is None


async def test_the_tail_is_filtered_by_verdict_not_by_extractable() -> None:
    """`extractable` is still written, but a second flag that can disagree
    with the verdict is the bug the design forbids — so nothing reads it."""
    session = FakeSession([])
    await store.unextracted_tail(session, chat_pk=1)
    rendered = str(session.statements[0])
    assert "message.verdict" in rendered
    assert "message.extractable" not in rendered


async def test_upsert_writes_the_verdict_on_a_new_row() -> None:
    session = FakeSession([None])
    row, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_QUESTION
    )
    assert created is True
    assert row.verdict == VERDICT_QUESTION


async def test_a_stored_q_is_not_a_fact() -> None:
    """The tail is selected by verdict from this commit on, and a /q row has
    content — so leaving it on the default verdict puts the user's own
    question into the extractor and coins a kind out of it. That is the
    taxonomy poisoning the whole design exists to prevent."""
    session = FakeSession([None])
    row, _ = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_QUESTION
    )
    assert row.verdict != VERDICT_FACT


async def test_upsert_overwrites_the_verdict_on_an_edit() -> None:
    """An edit can move a message from one verdict to another; a stale
    verdict would route the corrected message the way the typo read."""
    existing = LoggedMessage(
        id=42, chat_pk=1, message_id=10, kind=KIND_TEXT, text="4500 такси",
        tg_date=datetime(2026, 9, 11, 3, tzinfo=UTC), raw={},
        verdict=VERDICT_FACT,
    )
    session = FakeSession([existing])
    row, created = await store.upsert_message(
        session, Chat(id=1, chat_id=7), _msg(), verdict=VERDICT_SYSTEM
    )
    assert created is False
    assert row.verdict == VERDICT_SYSTEM
```

`FakeSession` already exists in `tests/test_store.py`; give it a
`self.statements` list appended to in `execute` if it does not have one, and add
a module-level `_msg()` returning the same `SimpleNamespace` shape
`tests/test_ingest.py::message` uses.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'VERDICTS'`.

- [ ] **Step 3: Add the constants and the column**

```python
# telegrind/models.py — after KIND_VOICE
#: What the classifier decided a message is, and therefore what happens to
#: it. Never null: a null would mean «not classified yet», «the classifier
#: failed» and «the classifier said it is not a fact» all at once, and the
#: flag would be undebuggable exactly when it misroutes. A classifier
#: failure writes VERDICT_FACT explicitly, which is the behaviour that
#: shipped before the classifier existed.
VERDICT_FACT = "fact"
VERDICT_QUESTION = "question"
VERDICT_TALK = "talk"
#: Rows the classifier never sees: every message the bot or Claude sends,
#: and every slash command that is not /q.
VERDICT_SYSTEM = "system"
VERDICTS = (VERDICT_FACT, VERDICT_QUESTION, VERDICT_TALK, VERDICT_SYSTEM)
```

```python
# telegrind/models.py — in LoggedMessage, after `extractable`
    #: What routing decided this message is. Derived, like extracted_at,
    #: and re-derived on an edit. It replaces `extractable` as the thing
    #: the tail is selected by; `extractable` is still written in step
    #: with it, and dropping that column is a later contract step.
    verdict: Mapped[str] = mapped_column(default=VERDICT_FACT, server_default="fact")
```

- [ ] **Step 4: Write the migration**

```bash
uv run alembic revision -m "message verdict"
```

Then fill it in — the generated `revision`/`down_revision` values stay as
generated, everything below the identifiers is replaced:

```python
"""message verdict

What routing decided a message is. It replaces `extractable` as the thing the
extraction tail is selected by, and it is never null.

This is the expand half of an expand/contract pair: `extractable` is left in
place and still written, so the downgrade is a plain drop and the rollback
described in the spec stays possible. Dropping `extractable` is a later
contract step, taken by hand once the verdict has held.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "<generated>"
down_revision: str | Sequence[str] | None = "509451c4c7ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "message",
        sa.Column("verdict", sa.String(), nullable=False, server_default="fact"),
    )
    # Backfill from what the old flag meant. A stored /q was a question and
    # is restored as one; every other unextractable row was a command or a
    # bot reply, which is `system`.
    op.execute(
        """
        UPDATE message SET verdict = CASE
            WHEN extractable THEN 'fact'
            WHEN text LIKE '/q%' THEN 'question'
            ELSE 'system'
        END
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("message", "verdict")
```

- [ ] **Step 5: Teach the store the verdict**

```python
# telegrind/store.py — import VERDICT_FACT alongside the others, then:

def reply_to(row: LoggedMessage) -> int | None:
    """The Telegram message_id this row replies to, if any.

    Read off `raw` rather than off a live aiogram object because Telegram
    does not nest replies: `reply_to_message.reply_to_message` is always
    None, so the second hop of a session lookup has to come from our own
    stored copy of the parent.
    """
    return ((row.raw or {}).get("reply_to_message") or {}).get("message_id")
```

```python
# telegrind/store.py — upsert_message signature and body
async def upsert_message(
    session: AsyncSession,
    chat: Chat,
    msg: Message,
    *,
    extractable: bool = True,
    verdict: str = VERDICT_FACT,
) -> tuple[LoggedMessage, bool]:
    ...
    if existing is not None:
        ...
        existing.extractable = extractable
        existing.verdict = verdict
        existing.extracted_at = None
        existing.extract_error = None
        return existing, False

    row = LoggedMessage(
        chat_pk=chat.id, extractable=extractable, verdict=verdict, **values
    )
```

```python
# telegrind/store.py — unextracted_tail: the one read that moves
        .where(
            LoggedMessage.chat_pk == chat_pk,
            LoggedMessage.verdict == VERDICT_FACT,
            LoggedMessage.extracted_at.is_(None),
            _has_content(),
        )
```

- [ ] **Step 6: Give `/q` its verdict**

`upsert_message` has three callers and the default writes `fact`. Two of them
want that; `/q` does **not**, and from this commit on the default would put the
user's own question into the extraction tail, because the tail is selected by
verdict and a `/q` row has content.

```python
# telegrind/bot/handlers/query.py — in `ask`
    async with session.begin():
        row, _ = await store.upsert_message(
            session, chat, message, extractable=False, verdict=VERDICT_QUESTION
        )
        row.receipt_emoji = RECEIPT_EMOJI
```

Import `VERDICT_QUESTION` from `telegrind.models`. The receipt stays 💔 for one
more commit — Task 5 is where a question stops getting one.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS — the whole suite, not just the new file.

- [ ] **Step 8: Apply the migration against the dev database**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: both directions clean. The round trip is the test that the rollback
path in the spec is real.

- [ ] **Step 9: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: the verdict column, and the tail reads it instead of extractable

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The bot stores what it says

Every message the bot or Claude sends is written to `message` with
`verdict=system`. It is load-bearing twice: a reply to something the bot said
resolves to a row, so `extract._line` stops emitting «ответ на сообщение вне
окна», and the second hop of the session lookup has something to read.

**Files:**
- Create: `telegrind/bot/outbound.py`
- Modify: `telegrind/extract.py`, `telegrind/bot/handlers/query.py`
- Test: `tests/test_outbound.py`, `tests/test_extract.py`

**Interfaces:**
- Consumes: `store.upsert_message(..., verdict=...)` from Task 1.
- Produces:
  `async outbound.say(bot, session, chat, text: str, *, reply_to: int | None = None) -> Message`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outbound.py
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telegrind.bot import outbound
from telegrind.models import VERDICT_SYSTEM, Chat


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(
            message_id=900 + len(self.sent),
            date=datetime(2026, 9, 12, 10, tzinfo=UTC),
            edit_date=None,
            text=text,
            caption=None,
            voice=None,
            forward_origin=None,
            chat=SimpleNamespace(id=chat_id),
            model_dump=lambda mode="json": {"text": text},
        )


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            yield

        return ctx()

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


async def test_what_the_bot_says_is_stored_as_system() -> None:
    session = FakeSession()
    await outbound.say(FakeBot(), session, Chat(id=1, chat_id=7), "Записал.")
    assert [(r.text, r.verdict, r.extractable) for r in session.added] == [
        ("Записал.", VERDICT_SYSTEM, False)
    ]


async def test_a_reply_goes_out_with_reply_parameters() -> None:
    """reply_to_message_id is deprecated at aiogram 3.27."""
    bot = FakeBot()
    await outbound.say(bot, FakeSession(), Chat(id=1, chat_id=7), "…", reply_to=42)
    assert bot.sent[0]["reply_parameters"].message_id == 42


async def test_a_plain_message_carries_no_reply_parameters() -> None:
    bot = FakeBot()
    await outbound.say(bot, FakeSession(), Chat(id=1, chat_id=7), "…")
    assert bot.sent[0].get("reply_parameters") is None
```

```python
# tests/test_extract.py — append
from telegrind.extract import author_of
from telegrind.models import VERDICT_SYSTEM, LoggedMessage


def test_a_stored_bot_message_is_not_attributed_to_the_user() -> None:
    """The bot's own messages are in the window now. Left as «я» they read
    as the user asserting whatever the bot said."""
    row = LoggedMessage(raw={}, verdict=VERDICT_SYSTEM)
    assert author_of(row, chat_id=7) == "бот"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_outbound.py tests/test_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.bot.outbound'`.

- [ ] **Step 3: Write `outbound.say`**

```python
# telegrind/bot/outbound.py
"""The only way the bot speaks.

Every outgoing message is stored, because a reply to something the bot said
has to resolve to a row: without one, `extract._line` calls it «ответ на
сообщение вне окна», and the second hop of a session lookup has nothing to
read. `verdict=system` keeps it out of the extraction tail — it is the bot's
own text, and a taxonomy coined from it would be the bot reading itself.
"""

from aiogram import Bot
from aiogram.types import Message, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.models import VERDICT_SYSTEM, Chat


async def say(
    bot: Bot,
    session: AsyncSession,
    chat: Chat,
    text: str,
    *,
    reply_to: int | None = None,
) -> Message:
    """Send, then store what was sent. Opens its own transaction.

    `reply_to_message_id` is deprecated at aiogram 3.27; ReplyParameters is
    the field the Bot API 9.6 schema actually carries.
    """
    sent = await bot.send_message(
        chat.chat_id,
        text,
        reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None,
    )
    async with session.begin():
        await store.upsert_message(
            session, chat, sent, extractable=False, verdict=VERDICT_SYSTEM
        )
    return sent
```

- [ ] **Step 4: Teach `author_of` about the bot**

```python
# telegrind/extract.py — at the top of author_of, before the forward_origin read
    if row.verdict == VERDICT_SYSTEM:
        # The bot's own messages are in the window since the outbound store
        # landed. Attributed to «я» they read as the user asserting them.
        return "бот"
```

Import `VERDICT_SYSTEM` from `telegrind.models` alongside `Chat, LoggedMessage`,
and extend `author_of`'s docstring with one line naming the fourth arm.

- [ ] **Step 5: Route `/q`'s two sends through it**

```python
# telegrind/bot/handlers/query.py — replace both bot.send_message calls
    if pending:
        await outbound.say(bot, session, chat, f"Разбираю {pending} сообщений…")

    async with session.begin():
        text = await answer_for(question_of(message.text), chat, config, session)
    await outbound.say(bot, session, chat, text, reply_to=message.message_id)
```

Import `from telegrind.bot import outbound`. The answer is sent as a reply from
here on: it is what makes a follow-up resolvable, and it costs nothing.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS. `tests/test_qhandler.py` will need its fake bot to return a
message object from `send_message` — update it to the `FakeBot` shape above
rather than inventing a second one.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: the bot stores what it says, and the extractor knows who said it

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The classifier

One cheap call, and the rules that need no call at all. Nothing consumes it yet.

**Files:**
- Create: `telegrind/classify.py`
- Modify: `telegrind/llm.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `llm.use_tool`, the verdict constants from Task 1.
- Produces: `classify.presumed(text: str | None) -> str | None`;
  `async classify.verdict_for(text: str | None, *, call=llm.use_tool) -> str`.
- Produces in `llm.py`: `CLASSIFY_SYSTEM: str`, `CLASSIFY_TOOL: ToolParam`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classify.py
from typing import Any

from telegrind import classify
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICT_TALK,
)


def answering(verdict: str) -> Any:
    async def call(system: str, user: str, tool: dict, **kwargs: Any) -> dict:
        return {"verdict": verdict}

    return call


def test_q_is_a_question_without_a_call() -> None:
    assert classify.presumed("/q сколько я потратил") == VERDICT_QUESTION


def test_any_other_command_is_system_without_a_call() -> None:
    assert classify.presumed("/start") == VERDICT_SYSTEM


def test_a_message_with_nothing_to_read_is_a_fact_without_a_call() -> None:
    """A sticker or a photo. `_has_content()` keeps it out of the tail
    anyway, so this is today's behaviour at no cost."""
    assert classify.presumed(None) == VERDICT_FACT
    assert classify.presumed("   ") == VERDICT_FACT


def test_ordinary_text_needs_the_model() -> None:
    assert classify.presumed("4500 такси") is None


async def test_the_model_decides_ordinary_text() -> None:
    assert await classify.verdict_for("почему это расход", call=answering("talk")) == (
        VERDICT_TALK
    )


async def test_a_presumed_verdict_makes_no_call() -> None:
    async def explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("the classifier was called for a command")

    assert await classify.verdict_for("/start", call=explode) == VERDICT_SYSTEM


async def test_a_classifier_failure_defaults_to_fact() -> None:
    """Storage is unconditional and must not come to depend on a model
    call: a hiccup costs a routing decision, never a message."""

    async def failing(*args: Any, **kwargs: Any) -> dict:
        raise RuntimeError("overloaded_error")

    assert await classify.verdict_for("4500 такси", call=failing) == VERDICT_FACT


async def test_a_verdict_the_model_invented_defaults_to_fact() -> None:
    assert await classify.verdict_for("x", call=answering("чепуха")) == VERDICT_FACT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.classify'`.

- [ ] **Step 3: Write the prompt and the tool**

```python
# telegrind/llm.py — after EXTRACT_TOOL
CLASSIFY_SYSTEM = """\
Ты сортируешь входящие сообщения личного дневника в Telegram. Ровно одна
категория на сообщение.

- `fact` — запись о том, что произошло: трата, измерение, привычка,
  событие, заметка. Сюда же всё, что похоже на запись, даже если непонятно
  на что именно: «4500 такси», «вес 82.4», «сходил в зал», «444».
- `question` — вопрос о том, что уже записано: сколько, когда, сколько раз,
  какой был последний. На такой вопрос отвечает база.
- `talk` — всё остальное, обращённое к боту: почему ты записал это так,
  измени правило, объясни, поговори. И просто разговор.

Если сомневаешься между `fact` и чем-то ещё — выбирай `fact`. Запись
дешевле переклассифицировать, чем потерять.
Если сомневаешься между `question` и `talk` — выбирай `question`: если
база не сможет ответить, вопрос всё равно уйдёт в разговор.
"""

CLASSIFY_TOOL: ToolParam = {
    "name": "classify_message",
    "description": "Отнести сообщение к одной категории.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["fact", "question", "talk"]}
        },
        "required": ["verdict"],
    },
}
```

- [ ] **Step 4: Write `classify.py`**

```python
# telegrind/classify.py
"""What a message is, and therefore what happens to it.

One call, and it happens *after* the row is committed: nothing written is
ever lost, and that invariant must not come to depend on a model call
succeeding. A failure therefore defaults to `fact` — 💔 goes on, the
message enters the tail, and the behaviour is exactly what shipped before
the classifier existed.

This is the only place the verdict is decided. The slash rule lives here
rather than in an aiogram filter because two rules that can disagree is a
bug found in production, not a design.
"""

import logging
from collections.abc import Awaitable, Callable

from telegrind import llm
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICT_TALK,
)

log = logging.getLogger(__name__)

_ASKED = (VERDICT_FACT, VERDICT_QUESTION, VERDICT_TALK)


def presumed(text: str | None) -> str | None:
    """The verdict that needs no model call, or None if one is needed.

    Three cases. `/q` is the explicit override and sets the verdict rather
    than bypassing it. Any other slash command is `system` — a command must
    never coin a kind. And a message with nothing readable (a sticker, a
    photo, a voice note before ASR) is a `fact` that `_has_content()`
    already keeps out of the tail, so the call would buy nothing.
    """
    body = (text or "").strip()
    if not body:
        return VERDICT_FACT
    if body.startswith("/"):
        command = body.split(maxsplit=1)[0].split("@")[0]
        return VERDICT_QUESTION if command == "/q" else VERDICT_SYSTEM
    return None


async def verdict_for(
    text: str | None,
    *,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> str:
    """One classification. Never raises."""
    decided = presumed(text)
    if decided is not None:
        return decided

    try:
        payload = await call(llm.CLASSIFY_SYSTEM, text or "", llm.CLASSIFY_TOOL)
    except Exception as exc:
        log.warning("classifier failed, defaulting to a fact: %s", exc)
        return VERDICT_FACT

    verdict = str(payload.get("verdict") or "")
    if verdict not in _ASKED:
        log.warning("classifier returned %r, defaulting to a fact", verdict)
        return VERDICT_FACT
    return verdict
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_classify.py -v`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: the classifier, and the rules that need no model call

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The answering half, and a refusal the caller can tell from an answer

Two changes, one commit, because one forces the other. `answer_for` returns the
refusal as a string today, so no caller can distinguish "the model said no such
aggregate" from "here is your number" — and the fall-through needs that. And
routing (Task 5) has to call `answer_for`, while `query.py` will have to call
routing, which is a cycle unless the answering half moves out of the module that
registers `/q`. `receipts.py` was split out of `handlers.py` for exactly this
reason; this is the same split.

`/q` behaves identically after this task.

**Files:**
- Create: `telegrind/bot/answering.py`
- Modify: `telegrind/bot/handlers/query.py`
- Test: `tests/test_qhandler.py`

**Interfaces:**
- Produces in `answering.py`:
  `REFUSAL = "Не понял вопрос, переформулируй."`;
  `EMPTY_QUESTION = "Спроси что-нибудь после /q."`;
  `question_of(text: str | None) -> str`;
  `async answer_for(question, chat, config, session, *, passes, spec_for, query_run, render, vocabulary) -> str | None`
  — `None` means the question did not fit the closed set of aggregates.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qhandler.py — change the imports at the top of the file
from telegrind.bot.answering import REFUSAL, answer_for, question_of
```

```python
# tests/test_qhandler.py — append
from telegrind import query


async def _no_vocabulary(*args: object, **kwargs: object) -> str:
    return ""


async def test_an_unanswerable_question_returns_none_not_prose() -> None:
    """The caller has to be able to hand it to Claude instead."""

    async def refusing(*args: object, **kwargs: object) -> object:
        raise query.Unanswerable("unknown aggregate ''")

    async def no_pass(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(failed=0, complaints=0)

    result = await answer_for(
        "почему ты записал это расходом",
        Chat(id=1, chat_id=7),
        CFG,
        FakeSession(),
        passes=no_pass,
        spec_for=refusing,
        vocabulary=_no_vocabulary,
    )
    assert result is None


def test_the_refusal_text_is_still_available_to_the_caller() -> None:
    assert REFUSAL == "Не понял вопрос, переформулируй."


def test_a_question_survives_the_move_without_its_command() -> None:
    assert question_of("/q сколько я потратил") == "сколько я потратил"
    assert question_of("сколько я потратил") == "сколько я потратил"
```

The last assertion is new behaviour: `question_of` now also has to cope with a
question that never had a `/q` in front of it, because Task 5 calls it on plain
text.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_qhandler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.bot.answering'`.

- [ ] **Step 3: Move the answering half out**

Create `telegrind/bot/answering.py` holding `question_of`, `_vocabulary` and
`answer_for` — the whole of `handlers/query.py` except the `@router.message`
handler, moved verbatim except for the three changes below.

```python
# telegrind/bot/answering.py
"""Question → numbers → prose. No Telegram in here, so it tests.

Split out of `handlers/query.py` because it registers nothing. Importing a
handler module is what registers its handlers, and `bot/routing.py` needs
this half — importing it from `query.py` would drag the `/q` registration
into a cycle with routing, which `query.py` also needs.
"""

#: What the bot says when the question does not fit the closed set of
#: aggregates and there is nobody to hand it to.
REFUSAL = "Не понял вопрос, переформулируй."
EMPTY_QUESTION = "Спроси что-нибудь после /q."


def question_of(text: str | None) -> str:
    """The question, with a leading /q stripped if the user used one.

    It now sees plain text too: a natural-language question never had a
    command in front of it. An @mention suffix is still tolerated.
    """
    body = (text or "").strip()
    if not body.startswith("/q"):
        return body
    _, _, rest = body.partition(" ")
    return rest.strip()
```

```python
# telegrind/bot/answering.py — answer_for's signature and its refusal arm
) -> str | None:
    """The whole answer as one string, or None if the question does not fit.

    None rather than the refusal text: `answer.spec_for` already refuses a
    question it cannot express rather than approximating it, and that
    refusal is a hand-off, not a dead end — but only a caller that can see
    it is a refusal can hand it anywhere.
    """
    if not question:
        return EMPTY_QUESTION

    ...
    try:
        spec = await spec_for(question, words, config, today)
    except query.Unanswerable as exc:
        log.info("unanswerable question in chat %s: %s", chat.chat_id, exc)
        return None
```

- [ ] **Step 4: Leave `query.py` with the handler only**

```python
# telegrind/bot/handlers/query.py — the whole file after the move
"""/q — the override, kept because it costs nothing.

The classifier takes questions now, so asking no longer requires a command.
/q stays because it is useful twice: when the user wants to be sure they are
asking, and when the classifier got it wrong. It sets the verdict rather
than bypassing it, which is why there is no second answering path here —
see bot/answering.py for that half.
"""

from telegrind.bot.answering import answer_for, question_of
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI, acknowledge
from telegrind.bot.outbound import say
from telegrind.bot.router import router
from telegrind.models import VERDICT_QUESTION


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
        row, _ = await store.upsert_message(
            session, chat, message, extractable=False, verdict=VERDICT_QUESTION
        )
        row.receipt_emoji = RECEIPT_EMOJI
    await acknowledge(bot, message.chat.id, message.message_id, RECEIPT_EMOJI)

    # Said in a transaction of its own, and outside the answering one: the
    # first /q after a quiet week pays for the week, and holding a write
    # transaction open across two model calls to announce that is the wrong
    # shape even at one user. A bare read here would autobegin and make the
    # `session.begin()` below raise «a transaction is already begun».
    async with session.begin():
        pending = len(await store.unextracted_tail(session, chat.id))
    if pending:
        await say(bot, session, chat, f"Разбираю {pending} сообщений…")

    async with session.begin():
        text = await answer_for(question_of(message.text), chat, config, session)
    await say(bot, session, chat, text or REFUSAL, reply_to=message.message_id)
```

This is a holding shape: Task 5 replaces the body below the first
`session.begin()` with one call to `route`, because `/q` and a plain question
have to take the same path or the fall-through exists twice.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS, and `/q` is byte-for-byte unchanged in what it says.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "refactor: the answering half leaves the handler, and a refusal is a None

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Routing — the receipt only on facts, and an answer on a question

The first commit the user can see. All three arms land together: a fact gets 💔,
a question gets an answer, and talk gets nothing yet because the Claude handler
is Tasks 7–9. That last gap is deliberate and matches the spec — «nothing yet»
is the queue, visible — and it is why steps 1 and 3 were folded: no commit ships
a question with neither a reaction nor an answer.

**Files:**
- Create: `telegrind/bot/routing.py`
- Modify: `telegrind/bot/handlers/handlers.py`, `telegrind/bot/handlers/receipts.py`,
  `telegrind/bot/handlers/query.py`
- Test: `tests/test_routing.py`, `tests/test_ingest.py`

**Interfaces:**
- Consumes: `classify.verdict_for`, `outbound.say`, `answering.answer_for`,
  `answering.question_of`, `answering.REFUSAL`, `receipts.acknowledge`,
  `store.unextracted_tail`.
- Produces: `receipts.HANDED_OVER = "👀"`;
  `async receipts.clear_receipt(bot, chat_id, message_id) -> None`;
  `async routing.route(message, row, chat, config, session, bot, *, hand_over=None) -> None`
  where `hand_over` is
  `Callable[[Message, LoggedMessage, Chat, AsyncSession, Bot], Awaitable[bool]]`
  returning True when the message was accepted by the meta layer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing.py
from types import SimpleNamespace
from typing import Any

from telegrind.bot import routing
from telegrind.bot.answering import REFUSAL
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_TALK,
    Chat,
    LoggedMessage,
)

CFG = SimpleNamespace(tz_offset=6, currency="KZT")


class Recorder:
    def __init__(self) -> None:
        self.reactions: list[tuple[int, str | None]] = []
        self.said: list[tuple[str, int | None]] = []
        self.handed: list[int] = []

    async def acknowledge(self, bot: Any, chat_id: int, message_id: int, emoji: str)\
            -> None:
        self.reactions.append((message_id, emoji))

    async def clear_receipt(self, bot: Any, chat_id: int, message_id: int) -> None:
        self.reactions.append((message_id, None))

    async def say(self, bot: Any, session: Any, chat: Any, text: str,
                  *, reply_to: int | None = None) -> Any:
        self.said.append((text, reply_to))
        return SimpleNamespace(message_id=901)


def row(verdict: str) -> LoggedMessage:
    return LoggedMessage(id=42, chat_pk=1, message_id=10, verdict=verdict, raw={})


def msg() -> SimpleNamespace:
    return SimpleNamespace(message_id=10, text="…", chat=SimpleNamespace(id=7))


async def test_a_fact_gets_the_receipt_and_no_words(monkeypatch: Any) -> None:
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    await routing.route(msg(), row(VERDICT_FACT), Chat(id=1, chat_id=7), CFG,
                        object(), object())

    assert rec.reactions == [(10, RECEIPT_EMOJI)]
    assert rec.said == []


async def test_a_question_is_answered_and_gets_no_receipt(monkeypatch: Any) -> None:
    """💔 promises that tapping deletes the facts on the message. A question
    has none, so the receipt would promise a gesture that does nothing."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def answered(*args: Any, **kwargs: Any) -> str:
        return "12 400 ₸."

    monkeypatch.setattr(routing, "answer_for", answered)

    await routing.route(msg(), row(VERDICT_QUESTION), Chat(id=1, chat_id=7), CFG,
                        FakeSession(), object())

    assert rec.reactions == []
    assert rec.said == [("12 400 ₸.", 10)]


async def test_a_refused_question_falls_through_to_the_handler(
    monkeypatch: Any,
) -> None:
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def refused(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routing, "answer_for", refused)

    async def hand_over(*args: Any, **kwargs: Any) -> bool:
        rec.handed.append(10)
        return True

    await routing.route(msg(), row(VERDICT_QUESTION), Chat(id=1, chat_id=7), CFG,
                        FakeSession(), object(), hand_over=hand_over)

    assert rec.handed == [10]
    assert rec.said == []


async def test_a_refused_question_still_says_so_when_there_is_nobody_to_ask(
    monkeypatch: Any,
) -> None:
    """Without this arm, folding steps 1 and 3 does not close the hole it
    was folded to close: the question would get silence."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    async def refused(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routing, "answer_for", refused)

    await routing.route(msg(), row(VERDICT_QUESTION), Chat(id=1, chat_id=7), CFG,
                        FakeSession(), object(), hand_over=None)

    assert rec.said == [(REFUSAL, 10)]


async def test_talk_with_nobody_to_hand_it_to_stays_bare(monkeypatch: Any) -> None:
    """«Nothing yet» is the queue, visible — and here the queue is waiting
    on a runtime that is not configured."""
    rec = Recorder()
    monkeypatch.setattr(routing, "acknowledge", rec.acknowledge)
    monkeypatch.setattr(routing, "say", rec.say)

    await routing.route(msg(), row(VERDICT_TALK), Chat(id=1, chat_id=7), CFG,
                        FakeSession(), object(), hand_over=None)

    assert rec.reactions == []
    assert rec.said == []
```

`FakeSession` is the one from `tests/test_outbound.py`; import it or copy the
four-method class — it is four methods and copying beats a shared fixtures
module for one more user.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_routing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.bot.routing'`.

- [ ] **Step 3: Add the two receipt primitives**

```python
# telegrind/bot/handlers/receipts.py — after RECEIPT_EMOJI
#: What a message handed to Claude gets. Deliberately not a fourth heart:
#: every emoji in RECEIPT_CYCLE means «tapping this deletes the facts on
#: this message», and a handed-over message has none. The gesture and the
#: family of emoji that carries it stay matched.
HANDED_OVER = "👀"
```

```python
# telegrind/bot/handlers/receipts.py — after acknowledge
async def clear_receipt(bot: Bot, chat_id: int, message_id: int) -> None:
    """Take the reaction off. Never fatal, for the same reason.

    An empty reaction list is how setMessageReaction clears; it is what an
    edit that turns a fact into talk needs, because the receipt left behind
    would promise a delete gesture the message no longer has.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=[]
        )
    except Exception:  # cosmetic, and the row is already safe
        log.warning("could not clear the receipt reaction on %s", message_id)
```

- [ ] **Step 4: Write `routing.py`**

```python
# telegrind/bot/routing.py
"""The verdict decides: a receipt, an answer, or a hand-off.

The one place the three arms meet, and the seam the meta layer plugs into —
`hand_over` is passed in, so nothing here imports `telegrind.meta` and
removing the meta layer is deleting one argument at the call site.

The receipt is the routing signal. 💔 means «understood as a fact, will
extract it», and tapping it deletes. 👀 means «handed to Claude», placed by
the meta layer when the turn starts rather than here, so the queue is legible
on screen: the messages still bare are the ones not yet seen.
"""

import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.answering import REFUSAL, answer_for, question_of
from telegrind.bot.handlers.receipts import RECEIPT_EMOJI, acknowledge
from telegrind.bot.outbound import say
from telegrind.config import ChatConfig
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_TALK,
    Chat,
    LoggedMessage,
)

log = logging.getLogger(__name__)

HandOver = Callable[
    [Message, LoggedMessage, Chat, AsyncSession, Bot], Awaitable[bool]
]


async def route(
    message: Message,
    row: LoggedMessage,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
    *,
    hand_over: HandOver | None = None,
    receipt: str = RECEIPT_EMOJI,
) -> None:
    """Act on a verdict. The row is already committed before this runs."""
    if row.verdict == VERDICT_FACT:
        await acknowledge(bot, chat.chat_id, message.message_id, receipt)
        return

    if row.verdict == VERDICT_TALK:
        if hand_over is None or not await hand_over(message, row, chat, session, bot):
            log.info("nobody to hand message %s to", message.message_id)
        return

    if row.verdict == VERDICT_QUESTION:
        # Said in a transaction of its own, and outside the answering one:
        # the first question after a quiet week pays for the week, and
        # holding a write transaction open across two model calls to
        # announce that is the wrong shape even at one user. A bare read
        # would autobegin and make the begin() below raise «a transaction
        # is already begun».
        async with session.begin():
            pending = len(await store.unextracted_tail(session, chat.id))
        if pending:
            await say(bot, session, chat, f"Разбираю {pending} сообщений…")

        async with session.begin():
            text = await answer_for(question_of(message.text), chat, config, session)
        if text is not None:
            await say(bot, session, chat, text, reply_to=message.message_id)
            return
        # The bot could not express it, so Claude does. A misroute across
        # the fact/question line then costs a second of latency instead of
        # an unanswered question — which is what lets the classifier's
        # boundary be soft.
        if hand_over is not None and await hand_over(message, row, chat, session, bot):
            return
        await say(bot, session, chat, REFUSAL, reply_to=message.message_id)
        return

    # VERDICT_SYSTEM: a command that is not /q, or the bot's own message.
    # Stored, and nothing else.
```

**Note for the implementer:** `routing.py` imports `telegrind.bot.answering`,
which registers nothing, and never `handlers/query.py`, which registers `/q`.
That direction is what keeps the import graph acyclic — `query.py` imports
`routing`, not the other way round. Do not move the `/q` registration.

- [ ] **Step 5: `/q` takes the same path**

The override has to set the verdict rather than bypass it, or the
fall-through exists in two places and they drift. And a `/q` no longer gets
💔: a question has no facts, so the receipt would promise a delete gesture
that does nothing — which is the spec's own reason for the change, and it
cannot apply to one kind of question and not the other.

```python
# telegrind/bot/handlers/query.py — the handler, in full
@router.message(Command("q"))
async def ask(
    message: Message,
    chat: Chat,
    config: ChatConfig,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Store the question, then route it exactly as a plain one is routed."""
    async with session.begin():
        row, _ = await store.upsert_message(
            session, chat, message, extractable=False, verdict=VERDICT_QUESTION
        )
        row.receipt_emoji = None

    await route(message, row, chat, config, session, bot, hand_over=HAND_OVER)
```

`HAND_OVER` lives on `telegrind.bot.handlers.handlers` and is read at call
time, so `query.py` reads it through the module rather than importing the
name: `from telegrind.bot.handlers import handlers` then
`hand_over=handlers.HAND_OVER`. Importing `handlers` from `query` reverses the
registration order `handlers/__init__.py` depends on — so do it inside the
function body, with a one-line comment saying why.

- [ ] **Step 6: Collapse ingestion onto one handler**

```python
# telegrind/bot/handlers/handlers.py — replace record_command/record_voice/record_text
"""Ingestion. Store, commit, classify, and act on the verdict.

There is no echo on the fact path: the confirmation that the bot understood
is 💔, and that reaction is also the delete affordance. What the classifier
decides is what happens next — see bot/routing.py.

The row is committed *before* the classifier runs, and the classifier's
failure mode is `fact`. Nothing written is ever lost, and that must not come
to depend on a model call.

There is no COMMAND_LIKE filter any more. The slash rule lives in
`classify.presumed`, because two rules that can disagree about whether a
message is a command is a bug found in production.
"""

from telegrind import classify, extract, store
from telegrind.bot.handlers.receipts import (
    RECEIPT_CYCLE as RECEIPT_CYCLE,
)
from telegrind.bot.handlers.receipts import (
    HANDED_OVER,
    RECEIPT_EMOJI,
    acknowledge,
    clear_receipt,
    next_receipt,
)
from telegrind.bot.router import router
from telegrind.bot.routing import HandOver, route


@router.message()
async def record(
    message: Message, chat: Chat, config: ChatConfig, session: AsyncSession, bot: Bot
) -> None:
    """Store anything the user sent, then route it.

    No filter, deliberately: a sticker, a photo or a document is a message
    the user sent, so it is stored. `message_values` already handles a
    caption and a missing text, and `classify.presumed` gives a message
    with nothing readable a `fact` verdict without spending a call.
    """
    verdict = await classify.verdict_for(message.text or message.caption)
    async with session.begin():
        row, _ = await store.upsert_message(
            session,
            chat,
            message,
            extractable=verdict == VERDICT_FACT,
            verdict=verdict,
        )
        row.receipt_emoji = RECEIPT_EMOJI if verdict == VERDICT_FACT else None

    await route(message, row, chat, config, session, bot, hand_over=HAND_OVER)
```

**Import only what this task uses.** `HANDED_OVER` and `clear_receipt` are not
used until Task 6, and `ruff` fails an unused import — add them to the import
block there, not here. `VERDICT_FACT` comes from `telegrind.models`.

**The classifier call is outside the transaction on purpose** — it is a model
call, and `query.py:89-93` already records why a write transaction must not be
held across one. The row is written in the `session.begin()` that follows, which
is the commit the whole invariant rests on.

`HAND_OVER` is a module-level `HandOver | None`, `None` until Task 9 sets it:

```python
# telegrind/bot/handlers/handlers.py — near the top
#: Set by setup_dispatcher() when the meta layer is configured. None means
#: there is nobody to hand a message to, and routing says so rather than
#: going silent.
HAND_OVER: HandOver | None = None
```

- [ ] **Step 7: Update `tests/test_ingest.py`**

`record_command`, `record_voice` and `record_text` are gone. Replace the tests
that call them with calls to `record`, passing a fake `classify.verdict_for` via
`monkeypatch`. Keep every receipt-cycle test as it is — none of them touch the
handlers. Add:

```python
async def test_a_question_gets_no_receipt(monkeypatch: Any) -> None:
    async def question(*args: Any, **kwargs: Any) -> str:
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", question)
    routed: list[str] = []

    async def fake_route(*args: Any, **kwargs: Any) -> None:
        routed.append(args[1].verdict)

    monkeypatch.setattr(handlers, "route", fake_route)

    session = EditSession(None)
    await handlers.record(message(), Chat(id=1, chat_id=7), CFG, session, FakeBot())
    assert routed == [VERDICT_QUESTION]
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS.

- [ ] **Step 9: Walk it on the dev bot**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run python main.py
```

In the dev chat, send in order: `4500 такси` → expect 💔 and nothing said;
`сколько я потратил на такси` → expect **no** reaction and a number;
`почему ты это так записал` → expect no reaction and no reply (nobody to hand it
to yet); `/start` → expect no reaction and no reply. Then check the rows:

```sql
select message_id, verdict, extractable, receipt_emoji, left(text, 30)
from message order by id desc limit 8;
```

- [ ] **Step 10: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: the verdict routes the message, and the receipt goes only on facts

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: An edit is re-classified, and 👀 is the point of no return

**Files:**
- Modify: `telegrind/bot/handlers/handlers.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: everything from Task 5.
- Produces: nothing new; `record_edited` changes shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest.py — append
async def test_an_edit_after_the_handover_is_ignored(monkeypatch: Any) -> None:
    """👀 is the point of no return. The row is still overwritten — nothing
    written is ever lost — but nothing downstream reacts to it."""
    called: list[str] = []

    async def loud(*args: Any, **kwargs: Any) -> str:
        called.append("classified")
        return VERDICT_FACT

    monkeypatch.setattr(handlers.classify, "verdict_for", loud)

    async def fake_route(*args: Any, **kwargs: Any) -> None:
        called.append("routed")

    monkeypatch.setattr(handlers, "route", fake_route)

    existing = stored(extracted=False)
    existing.verdict = VERDICT_TALK
    existing.receipt_emoji = HANDED_OVER

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )

    assert called == []
    assert existing.text == "5500 такси"


async def test_an_edit_that_turns_a_fact_into_talk_clears_the_receipt(
    monkeypatch: Any,
) -> None:
    async def talk(*args: Any, **kwargs: Any) -> str:
        return VERDICT_TALK

    monkeypatch.setattr(handlers.classify, "verdict_for", talk)
    monkeypatch.setattr(handlers, "route", _noop_route)

    existing = stored(extracted=False)
    bot = FakeBot()
    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, bot
    )

    assert bot.reactions == [(7, 10, [])]
    assert existing.receipt_emoji is None


async def test_an_edit_that_stays_a_fact_advances_the_cycle(
    monkeypatch: Any,
) -> None:
    async def fact(*args: Any, **kwargs: Any) -> str:
        return VERDICT_FACT

    monkeypatch.setattr(handlers.classify, "verdict_for", fact)
    monkeypatch.setattr(handlers, "route", _noop_route)

    existing = stored(extracted=False)
    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(existing), CFG, FakeBot()
    )
    assert existing.receipt_emoji == "❤‍🔥"


async def test_an_edited_question_is_answered_again(monkeypatch: Any) -> None:
    """The typo-fix case. Without it, correcting a question gets neither a
    reaction nor an answer — the hole steps 1 and 3 were folded to close."""
    async def question(*args: Any, **kwargs: Any) -> str:
        return VERDICT_QUESTION

    monkeypatch.setattr(handlers.classify, "verdict_for", question)
    routed: list[str] = []

    async def fake_route(*args: Any, **kwargs: Any) -> None:
        routed.append(args[1].verdict)

    monkeypatch.setattr(handlers, "route", fake_route)

    await handlers.record_edited(
        edit(), Chat(id=1, chat_id=7), EditSession(stored(extracted=False)), CFG,
        FakeBot(),
    )
    assert routed == [VERDICT_QUESTION]
```

Add `async def _noop_route(*args: Any, **kwargs: Any) -> None: pass` to the
module. `FakeBot.set_message_reaction` already records
`(chat_id, message_id, [emoji…])`, so an empty list is the clear.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: FAIL — the edit handler still derives `parses` from a leading slash.

- [ ] **Step 3: Rewrite `record_edited`**

```python
@router.edited_message()
async def record_edited(
    edited_message: Message,
    chat: Chat,
    session: AsyncSession,
    config: ChatConfig,
    bot: Bot,
) -> None:
    """Overwrite the text, re-derive the verdict, and act on it.

    An edit can change the answer: «взял 3000» corrected to «потратил 3000
    на такси» is exactly the case where a fact moves from one kind to
    another, and correcting data or a typo is what edits are actually used
    for. So the verdict is re-derived on the same principle as extracted_at.

    👀 is the point of no return. A message already handed to Claude is
    still overwritten — nothing written is ever lost — but nothing
    downstream reacts to it: no re-send, no new turn, and no
    re-classification either. The correct behaviour would be to rewind the
    session, which is transcript surgery on a .jsonl we do not own, for a
    gesture whose workaround is saying the correction out loud.

    The extracted-or-not check has to happen *before* upsert_message, which
    clears extracted_at by design — and inside a transaction of its own, or
    the next `session.begin()` raises on the autobegun one.
    """
    # Three blocks, and the boundaries are load-bearing. A bare read
    # autobegins, so `previous` cannot be fetched outside a transaction or
    # the `session.begin()` below raises «a transaction is already begun» —
    # the same trap query.py documents. And the classifier call must sit
    # between two transactions, never inside one.
    async with session.begin():
        previous = await store.get_message(
            session, chat.id, edited_message.message_id
        )
        handed_over = previous is not None and previous.receipt_emoji == HANDED_OVER
        was_extracted = previous is not None and previous.extracted_at is not None
        was_fact = previous is not None and previous.verdict == VERDICT_FACT
        previous_verdict = previous.verdict if previous is not None else VERDICT_FACT

    if handed_over:
        async with session.begin():
            await store.upsert_message(
                session,
                chat,
                edited_message,
                extractable=False,
                verdict=previous_verdict,
            )
        log.info("edit after hand-over on %s, ignored", edited_message.message_id)
        return

    verdict = await classify.verdict_for(
        edited_message.text or edited_message.caption
    )

    async with session.begin():
        row, _ = await store.upsert_message(
            session,
            chat,
            edited_message,
            extractable=verdict == VERDICT_FACT,
            verdict=verdict,
        )
        if verdict == VERDICT_FACT:
            # A first sighting as a fact gets the default; a message that
            # was already one gets the next emoji along, and that change is
            # the only thing telling the user the bot saw the edit.
            emoji = next_receipt(row.receipt_emoji) if was_fact else RECEIPT_EMOJI
        else:
            emoji = None
        row.receipt_emoji = emoji

        if was_extracted and verdict == VERDICT_FACT:
            report = await extract.run_for(session, chat, config, row)
            log.info("re-extracted message %s: %s fact(s)", row.id, report.facts)

    if emoji is None and was_fact:
        # The receipt promised a delete gesture the message no longer has.
        await clear_receipt(bot, chat.chat_id, edited_message.message_id)

    await route(
        edited_message, row, chat, config, session, bot,
        hand_over=HAND_OVER, receipt=emoji or RECEIPT_EMOJI,
    )
```

**Note:** `route` places the receipt on the fact arm, so passing `receipt=emoji`
is what makes the cycle advance rather than re-place 💔. The `clear_receipt`
call is separate because `route`'s talk arm deliberately does not touch
reactions — 👀 belongs to the meta layer.

**One behaviour this settles that the spec left open:** an edited *question* is
answered again. Task 5's `route` does that for free, because the question arm
does not look at whether this is a first sighting. Without it, fixing a typo in
a question produces neither a reaction nor an answer — the exact hole the fold
of steps 1 and 3 exists to close.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS.

- [ ] **Step 5: Walk the edit paths on the dev bot**

Send `4500 такси` (💔), edit it to `4600 такси` → ❤‍🔥. Edit it again to
`а почему это вообще расход` → the reaction disappears. Send
`сколько я потратил` → an answer; edit it to `сколько я потратил вчера` → a
second answer.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: an edit is re-classified, and the handover is the point of no return

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The meta layer's config and its session arithmetic

Pure functions and one row lookup. Nothing spawns anything yet.

**Files:**
- Create: `telegrind/meta/__init__.py`, `telegrind/meta/config.py`,
  `telegrind/meta/sessions.py`
- Test: `tests/test_meta_sessions.py`

**Interfaces:**
- Produces:
  `MetaConfig(admin_chat_ids: frozenset[int], binary: str, cwd: str, timeout: float)`
  with `MetaConfig.from_env() -> MetaConfig | None`;
  `sessions.session_id(chat_id: int, message_id: int) -> uuid.UUID`;
  `async sessions.turn_key(message, *, parent_of) -> int` where
  `parent_of: Callable[[int], Awaitable[int | None]]` answers «what did message
  N reply to», looked up in our own rows.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meta_sessions.py
import uuid
from types import SimpleNamespace
from typing import Any

from telegrind.meta import sessions
from telegrind.meta.config import MetaConfig


def msg(message_id: int, parent: Any = None) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, reply_to_message=parent)


def bot_msg(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(is_bot=True),
        reply_to_message=None,
    )


def user_msg(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(is_bot=False),
        reply_to_message=None,
    )


async def nothing(message_id: int) -> int | None:
    return None


def test_the_session_id_is_derived_and_stable() -> None:
    """Nothing is stored to make sessions work, so the session graph cannot
    drift from the chat."""
    first = sessions.session_id(7, 10)
    assert first == sessions.session_id(7, 10)
    assert first != sessions.session_id(7, 11)
    assert first != sessions.session_id(8, 10)
    assert isinstance(first, uuid.UUID)


async def test_a_plain_message_is_its_own_turn() -> None:
    """No parent, so it starts a new conversation — which falls out of the
    mechanism instead of needing a rule."""
    assert await sessions.turn_key(msg(10), parent_of=nothing) == 10


async def test_a_reply_to_ones_own_message_is_that_message() -> None:
    assert await sessions.turn_key(msg(11, user_msg(10)), parent_of=nothing) == 10


async def test_a_reply_to_a_bot_message_is_what_the_bot_replied_to() -> None:
    """Telegram does not nest replies, so the second hop reads our own row
    for the bot's message — which exists because the bot stores what it
    says."""

    async def parent_of(message_id: int) -> int | None:
        return {901: 10}.get(message_id)

    assert await sessions.turn_key(msg(11, bot_msg(901)), parent_of=parent_of) == 10


async def test_a_bot_message_with_no_stored_parent_is_its_own_turn() -> None:
    """A bot message sent before the outbound store existed, or one sent
    without reply parameters. Starting a fresh conversation is the honest
    failure."""
    assert await sessions.turn_key(msg(11, bot_msg(901)), parent_of=nothing) == 901


def test_no_allowlist_means_no_meta_layer(monkeypatch: Any) -> None:
    monkeypatch.delenv("CLAUDE_ADMIN_CHAT_IDS", raising=False)
    assert MetaConfig.from_env() is None


def test_the_allowlist_is_read_off_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("CLAUDE_ADMIN_CHAT_IDS", "7, 8")
    cfg = MetaConfig.from_env()
    assert cfg is not None
    assert cfg.allows(7) and cfg.allows(8) and not cfg.allows(9)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_meta_sessions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.meta'`.

- [ ] **Step 3: Write the config**

```python
# telegrind/meta/config.py
"""How the meta layer is configured. One bot's worth, read from env.

Per-bot configuration is deferred by the design, but the coupling is not:
the token, the working directory and the allowlist are values here rather
than constants, so splitting this module out later is deleting a wiring
file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MetaConfig:
    """The admin allowlist, and where `claude` lives.

    The bot has no sender gate: populate_chat_data resolves — or creates —
    a Chat row for whatever chat_id arrives. That is harmless while the bot
    only records and counts, and stops being harmless the moment text from
    a chat can commit, push and restart production. So the gate is here,
    and it is on the chat rather than on the verdict: `talk` from a stranger
    is not answered at all.
    """

    admin_chat_ids: frozenset[int]
    binary: str = "claude"
    cwd: str = "."
    #: Seconds. A crashed or hung turn breaks the promise 👀 made, so the
    #: turn is bounded and the failure is said out loud.
    timeout: float = 600.0

    def allows(self, chat_id: int) -> bool:
        return chat_id in self.admin_chat_ids

    @classmethod
    def from_env(cls) -> MetaConfig | None:
        """None when CLAUDE_ADMIN_CHAT_IDS is unset or empty.

        None is a supported state, not a misconfiguration: the recording
        half runs unchanged without it, and the dev container has no
        `claude` binary by design.
        """
        raw = os.getenv("CLAUDE_ADMIN_CHAT_IDS", "").strip()
        ids = frozenset(
            int(part) for part in raw.replace(",", " ").split() if part
        )
        if not ids:
            return None
        return cls(
            admin_chat_ids=ids,
            binary=os.getenv("CLAUDE_BIN", "claude"),
            # `or`, not a default: .env.dist ships the key with an empty
            # value, and getenv's default never fires for a set-but-empty
            # var — which would hand create_subprocess_exec cwd="".
            cwd=os.getenv("CLAUDE_CWD") or str(Path.cwd()),
            timeout=float(os.getenv("CLAUDE_TURN_TIMEOUT", "600")),
        )
```

- [ ] **Step 4: Write the session arithmetic**

```python
# telegrind/meta/sessions.py
"""A session is a reply chain, and it is never walked.

One hop off `reply_to_message`, at most one more off our own stored row for
the parent. The chain lives in the transcripts; the database only ever
answers «what is the parent». Nothing is stored to make this work — the id
is derived, so the session graph cannot drift from what Telegram shows the
user.

Reply means two different things and they separate mechanically: a reply to
one's own message is fact chaining, a reply to a Claude message continues the
conversation. Because a recorded fact gets no text reply, there is nothing of
Claude's to reply to on the recording path, and the two cannot collide.
"""

import uuid
from collections.abc import Awaitable, Callable

from aiogram.types import Message

#: A fixed namespace so the derivation is reproducible across restarts and
#: across machines. Generated once with uuid4 and frozen.
NAMESPACE = uuid.UUID("7a9f2d1e-5c34-4b8a-9e61-0d3f8c2a7b45")


def session_id(chat_id: int, message_id: int) -> uuid.UUID:
    """The session a turn writes to: the user message that caused it."""
    return uuid.uuid5(NAMESPACE, f"{chat_id}:{message_id}")


async def turn_key(
    message: Message,
    *,
    parent_of: Callable[[int], Awaitable[int | None]],
) -> int:
    """Which turn this message continues, as a Telegram message_id.

    Two hops at most. The parent is the user's own message → that is the
    turn. The parent is a bot message → the turn is the message *it*
    replied to, which exists because the bot always sends with reply
    parameters. Both land on the same place, which is the point: replying
    to one's own question and replying to the answer are the same place in
    the conversation, and the user should not have to know which continues
    it.

    Telegram does not nest replies — `reply_to_message.reply_to_message` is
    always None — so the second hop is `parent_of`, a lookup in our own
    rows.
    """
    parent = message.reply_to_message
    if parent is None:
        return message.message_id

    sender = getattr(parent, "from_user", None)
    if sender is not None and sender.is_bot:
        grandparent = await parent_of(parent.message_id)
        return grandparent if grandparent is not None else parent.message_id

    return parent.message_id
```

```python
# telegrind/meta/__init__.py
"""The Claude meta layer: a handler, not a second process.

It sees the update, it owns the conversation, it spawns `claude -p` and it
sends the reply. It is a guest in whatever bot registers it — it takes a
config object and callables, and reaches into none of the host's internals.
"""
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_meta_sessions.py -v`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: the meta layer's allowlist and its derived session ids

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: One turn of `claude -p`

The subprocess, the fork flag, the timeout, the exit code, the JSON. No
Telegram, so it tests.

**Files:**
- Create: `telegrind/meta/runtime.py`
- Modify: `telegrind/llm.py`
- Test: `tests/test_meta_runtime.py`

**Interfaces:**
- Consumes: `MetaConfig`, `sessions.session_id`.
- Produces: `runtime.TurnResult(text: str, ok: bool)`;
  `runtime.argv(cfg, prompt, *, session_id, resume_from) -> list[str]`;
  `async runtime.run_turn(cfg, prompt, *, session_id, resume_from=None, spawn=...) -> TurnResult`.
- Produces in `llm.py`: `META_SYSTEM: str`.

**Measured on this box 2026-09-12** (`claude` 2.1.269): `--output-format json`
returns one object with `result` (the text), `is_error`, `subtype` and
`session_id`. A cold turn cost $0.207 against 20 046 cache-creation tokens —
which is the number the warm base in step 5 exists to remove.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meta_runtime.py
import json
import uuid
from typing import Any

from telegrind.meta import runtime
from telegrind.meta.config import MetaConfig

CFG = MetaConfig(admin_chat_ids=frozenset({7}), binary="/usr/bin/claude", cwd="/app")
NEW = uuid.UUID("11111111-1111-5111-8111-111111111111")
BASE = uuid.UUID("22222222-2222-5222-8222-222222222222")


def test_a_fresh_conversation_names_its_own_session() -> None:
    argv = runtime.argv(CFG, "привет", session_id=NEW, resume_from=None)
    assert argv[0] == "/usr/bin/claude"
    assert "--session-id" in argv and str(NEW) in argv
    assert "--resume" not in argv
    assert argv[-1] == "привет"


def test_a_continued_turn_forks_off_the_parent() -> None:
    """Resume-in-place breaks at the third turn: the two-hop rule resolves
    to a message whose session was never created. Forking is what makes the
    derived id self-consistent."""
    argv = runtime.argv(CFG, "а почему", session_id=NEW, resume_from=BASE)
    assert argv[argv.index("--resume") + 1] == str(BASE)
    assert "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == str(NEW)


def test_permissions_are_bypassed_and_nobody_answers_prompts() -> None:
    """Without --permission-prompts none, anything that would have prompted
    is silently denied instead."""
    argv = runtime.argv(CFG, "x", session_id=NEW, resume_from=None)
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--permission-prompts") + 1] == "none"


def spawning(payload: dict[str, Any], code: int = 0, stderr: str = "") -> Any:
    async def spawn(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
        return code, json.dumps(payload), stderr

    return spawn


async def test_a_successful_turn_returns_its_text() -> None:
    result = await runtime.run_turn(
        CFG, "x", session_id=NEW,
        spawn=spawning({"result": "Записал.", "is_error": False}),
    )
    assert result == runtime.TurnResult(text="Записал.", ok=True)


async def test_a_nonzero_exit_is_said_out_loud() -> None:
    """A silence the user cannot tell from thinking is worse than an error
    message — 👀 is a promise."""
    result = await runtime.run_turn(
        CFG, "x", session_id=NEW, spawn=spawning({}, code=1, stderr="boom"),
    )
    assert result.ok is False
    assert "boom" in result.text


async def test_a_timeout_is_said_out_loud() -> None:
    async def hanging(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
        raise TimeoutError

    result = await runtime.run_turn(CFG, "x", session_id=NEW, spawn=hanging)
    assert result.ok is False
    assert "не уложился" in result.text


async def test_unparseable_output_is_a_failure_not_a_reply() -> None:
    async def garbage(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
        return 0, "not json", ""

    result = await runtime.run_turn(CFG, "x", session_id=NEW, spawn=garbage)
    assert result.ok is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_meta_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.meta.runtime'`.

- [ ] **Step 3: Write the runtime**

```python
# telegrind/meta/runtime.py
"""One `claude -p` per turn, forked from the session it is answering.

This is full Claude Code, not a trimmed one: the same CLAUDE.md, skills,
hooks and MCP servers the user has in the TUI. The fork is what keeps the
derived session id self-consistent — a turn is named by the user message
that caused it, and every turn writes to its own id, so two replies to
genuinely different points cannot interleave in one transcript.

Measured 2026-09-12 on this box (claude 2.1.269): `--output-format json`
returns one object carrying `result`, `is_error` and `session_id`.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegrind import llm
from telegrind.meta.config import MetaConfig

log = logging.getLogger(__name__)

Spawn = Callable[[list[str], str, float], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    text: str
    ok: bool


def argv(
    cfg: MetaConfig,
    prompt: str,
    *,
    session_id: uuid.UUID,
    resume_from: uuid.UUID | None,
) -> list[str]:
    """The command line for one turn.

    A fresh conversation names its own session. A continued one forks off
    the parent's: `--resume <base> --fork-session --session-id <new>` keeps
    the history and writes it under the id we name.
    """
    line = [
        cfg.binary,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        # Without this, anything that would have prompted is silently
        # denied instead of being answered.
        "--permission-prompts",
        "none",
        "--append-system-prompt",
        llm.META_SYSTEM,
    ]
    if resume_from is not None:
        line += ["--resume", str(resume_from), "--fork-session"]
    line += ["--session-id", str(session_id), prompt]
    return line


async def _spawn(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(), err.decode()


async def run_turn(
    cfg: MetaConfig,
    prompt: str,
    *,
    session_id: uuid.UUID,
    resume_from: uuid.UUID | None = None,
    spawn: Spawn = _spawn,
) -> TurnResult:
    """Run one turn. Never raises: 👀 is a promise, and a crashed or hung
    process breaks it silently unless the failure is said out loud."""
    line = argv(cfg, prompt, session_id=session_id, resume_from=resume_from)
    try:
        code, out, err = await spawn(line, cfg.cwd, cfg.timeout)
    except TimeoutError:
        log.warning("turn %s did not finish in %ss", session_id, cfg.timeout)
        return TurnResult(
            text=f"Не уложился в {int(cfg.timeout)} секунд — попробуй ещё раз.",
            ok=False,
        )
    except OSError as exc:
        log.warning("could not start %s: %s", cfg.binary, exc)
        return TurnResult(text=f"Не смог запуститься: {exc}", ok=False)

    if code != 0:
        log.warning("turn %s exited %s: %s", session_id, code, err.strip())
        return TurnResult(text=f"Упал с кодом {code}: {err.strip()[:400]}", ok=False)

    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        log.warning("turn %s answered with something that is not JSON", session_id)
        return TurnResult(text="Ответ пришёл в нечитаемом виде.", ok=False)

    if payload.get("is_error"):
        return TurnResult(text=str(payload.get("result") or "Ошибка."), ok=False)

    return TurnResult(text=str(payload.get("result") or ""), ok=True)
```

```python
# telegrind/llm.py — after ANSWER_SYSTEM
META_SYSTEM = """\
Ты отвечаешь в личном чате в Telegram. Это разговор, а не эссе.

- Говори коротко: одна-две фразы, разворачивай только если попросили.
- Никаких стен текста — их никто не читает с телефона.
- Если ответ не помещается в разговор (отчёт, таблица, дифф) — скажи об
  этом и предложи выгрузить файлом, а не дроби на три сообщения.
- Жёсткий предел одного сообщения — 4096 символов.
"""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_meta_runtime.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the fork flag against the real binary**

The unit tests never spawn a process, so check the one measured claim by hand:

```bash
BASE=$(uuidgen) && NEW=$(uuidgen)
claude -p --output-format json --session-id "$BASE" \
  --permission-mode bypassPermissions --permission-prompts none \
  'Remember the word BANANA. Reply OK.' | python -c 'import json,sys; print(json.load(sys.stdin)["result"])'
claude -p --output-format json --resume "$BASE" --fork-session --session-id "$NEW" \
  --permission-mode bypassPermissions --permission-prompts none \
  'What word did I ask you to remember?' | python -c 'import json,sys; print(json.load(sys.stdin)["result"])'
ls ~/.claude/projects/*/ | grep -c "$NEW"
```
Expected: the second answer says BANANA, and a transcript exists under the
forked id. If it does not, stop — the whole session design rests on it.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: one claude -p per turn, forked from the session it answers

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: The queue, the 👀, and the wiring

One turn at a time per session key; everything else runs concurrently. This is
the task that makes the bot talk.

**Files:**
- Create: `telegrind/meta/queue.py`, `telegrind/bot/meta_wiring.py`
- Modify: `telegrind/meta/__init__.py`, `telegrind/bot/setup.py`, `main.py`
- Test: `tests/test_meta_queue.py`

**Interfaces:**
- Consumes: `MetaConfig` and `sessions` (Task 7), `runtime.run_turn` (Task 8),
  `routing.HandOver` (Task 5), `store.reply_to` (Task 1), `outbound.say`
  (Task 2), `receipts.HANDED_OVER` and `receipts.clear_receipt` (Task 5).
- Produces:
  `queue.Job(chat_id: int, chat_pk: int, message_id: int, text: str, key: int)`;
  `queue.Turns(cfg, *, run, deliver, mark)` with `async submit(job) -> None`
  and `async drain() -> None` (tests only — waits for every worker to idle);
  `meta.MetaLayer(cfg, *, async_session, parent_of, set_receipt, speak)` whose
  `hand_over` method matches `routing.HandOver`;
  `meta_wiring.parent_of`, `meta_wiring.set_receipt`, `meta_wiring.speak`;
  `setup_dispatcher(async_session=None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meta_queue.py
import asyncio
import uuid
from typing import Any

from telegrind.meta.config import MetaConfig
from telegrind.meta.queue import Job, Turns
from telegrind.meta.runtime import TurnResult

CFG = MetaConfig(admin_chat_ids=frozenset({7}))


def job(message_id: int, key: int, text: str = "x") -> Job:
    return Job(chat_id=7, chat_pk=1, message_id=message_id, text=text, key=key)


class Spy:
    def __init__(self, result: str = "ok", gate: asyncio.Event | None = None) -> None:
        self.prompts: list[str] = []
        self.delivered: list[tuple[str, int]] = []
        self.marked: list[tuple[int, bool]] = []
        self.result = result
        self.gate = gate

    async def run(self, prompt: str, *, session_id: uuid.UUID,
                  resume_from: uuid.UUID | None) -> TurnResult:
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        return TurnResult(text=self.result, ok=True)

    async def deliver(self, job: Job, text: str) -> None:
        self.delivered.append((text, job.message_id))

    async def mark(self, job: Job, started: bool) -> None:
        self.marked.append((job.message_id, started))


async def test_a_turn_is_marked_when_it_starts_not_when_it_is_queued() -> None:
    """👀 goes on when the message is handed to the process, so the queue is
    legible on screen: the messages still bare are the ones not yet seen."""
    spy = Spy()
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.marked[0] == (10, True)


async def test_the_answer_replies_to_the_message_that_caused_it() -> None:
    spy = Spy(result="Потому что.")
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.delivered == [("Потому что.", 10)]


async def test_two_messages_queued_before_the_turn_starts_go_in_together() -> None:
    """A correction sent two seconds later belongs to the same thought, and
    one turn seeing both answers better than two turns each seeing half."""
    gate = asyncio.Event()
    spy = Spy(gate=gate)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10, text="первое"))
    await turns.submit(job(11, key=10, text="второе"))
    gate.set()
    await turns.drain()

    assert len(spy.prompts) == 1
    assert "первое" in spy.prompts[0] and "второе" in spy.prompts[0]
    assert {m for m, _ in spy.marked} == {10, 11}
    assert spy.delivered == [("ok", 11)]


async def test_two_different_keys_run_concurrently() -> None:
    """Different fork points are different sessions; serialising them would
    merge two threads the user deliberately split."""
    gate = asyncio.Event()
    spy = Spy(gate=gate)
    turns = Turns(CFG, run=spy.run, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.submit(job(20, key=20))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(spy.prompts) == 2
    gate.set()
    await turns.drain()


async def test_a_failed_turn_takes_the_eyes_off() -> None:
    async def failing(prompt: str, **kwargs: Any) -> TurnResult:
        return TurnResult(text="Упал.", ok=False)

    spy = Spy()
    turns = Turns(CFG, run=failing, deliver=spy.deliver, mark=spy.mark)
    await turns.submit(job(10, key=10))
    await turns.drain()
    assert spy.marked[-1] == (10, False)
    assert spy.delivered == [("Упал.", 10)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_meta_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.meta.queue'`.

- [ ] **Step 3: Write the queue**

```python
# telegrind/meta/queue.py
"""One turn at a time per session, and the rest queue.

The queue is keyed by the session id, and that one rule covers every case.
Same key, wait; different key, run now — because different fork points are
different sessions, and serialising them would merge two threads the user
deliberately split.

What happens to a follow-up depends only on whether the turn has started.
Not handed over yet: the two go in together as one prompt. Already running:
the follow-up waits, and then runs as the next turn on that session — which
is not the consolation prize, because by then the answer to the first
message exists and the follow-up is answered in light of it.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegrind.meta import sessions
from telegrind.meta.config import MetaConfig
from telegrind.meta.runtime import TurnResult

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Job:
    chat_id: int
    chat_pk: int
    message_id: int
    text: str
    #: The turn this message continues, as a Telegram message_id.
    key: int


Run = Callable[..., Awaitable[TurnResult]]
Deliver = Callable[[Job, str], Awaitable[None]]
#: `mark(job, True)` when the turn starts, `mark(job, False)` when it failed.
#: A bool rather than an emoji: the receipt vocabulary is the host's, and a
#: second copy of "👀" in here is the kind of duplicate that drifts.
Mark = Callable[[Job, bool], Awaitable[None]]


class Turns:
    """One worker task per live session key."""

    def __init__(self, cfg: MetaConfig, *, run: Run, deliver: Deliver, mark: Mark):
        self._cfg = cfg
        self._run = run
        self._deliver = deliver
        self._mark = mark
        self._queues: dict[tuple[int, int], asyncio.Queue[Job]] = {}
        self._workers: dict[tuple[int, int], asyncio.Task[None]] = {}

    async def submit(self, job: Job) -> None:
        slot = (job.chat_id, job.key)
        queue = self._queues.setdefault(slot, asyncio.Queue())
        await queue.put(job)
        worker = self._workers.get(slot)
        if worker is None or worker.done():
            self._workers[slot] = asyncio.create_task(self._work(slot, queue))

    async def drain(self) -> None:
        """Wait for every worker to finish. Tests only."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()))
            self._workers = {k: t for k, t in self._workers.items() if not t.done()}

    async def _work(self, slot: tuple[int, int], queue: asyncio.Queue[Job]) -> None:
        try:
            await self._loop(slot, queue)
        finally:
            # A chat runs for months; a dict that only ever grows is a leak
            # with a slow fuse. `submit` recreates both on the next message.
            if queue.empty():
                self._queues.pop(slot, None)
                self._workers.pop(slot, None)

    async def _loop(self, slot: tuple[int, int], queue: asyncio.Queue[Job]) -> None:
        while True:
            try:
                batch = [queue.get_nowait()]
            except asyncio.QueueEmpty:
                return
            # Everything already waiting for this session belongs to the
            # same thought: it arrived before the turn started, so it goes
            # in with it rather than becoming a second turn.
            while True:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            await self._turn(slot, batch)

    async def _turn(self, slot: tuple[int, int], batch: list[Job]) -> None:
        last = batch[-1]
        for job in batch:
            await self._mark(job, True)

        chat_id, key = slot
        # The key names the turn this batch continues. When that message is
        # itself in the batch, the conversation is starting and there is
        # nothing to resume; otherwise the key names an earlier turn whose
        # session id we fork off.
        fresh = key in {job.message_id for job in batch}
        resume_from = None if fresh else sessions.session_id(chat_id, key)
        session_id = sessions.session_id(chat_id, last.message_id)

        prompt = "\n".join(job.text for job in batch)
        try:
            result = await self._run(
                prompt, session_id=session_id, resume_from=resume_from
            )
            if not result.ok and resume_from is not None:
                # The base session may not exist: a merged batch names its
                # session after the last message, so the key's own id was
                # never written — and a transcript can also be pruned, or
                # the warm base rebuilt. Starting fresh loses the thread;
                # a crash string loses the answer.
                log.info("could not resume %s, starting fresh", resume_from)
                result = await self._run(
                    prompt, session_id=session_id, resume_from=None
                )
        except Exception as exc:  # a worker task that dies takes the queue with it
            log.exception("turn on %s blew up", session_id)
            result = TurnResult(text=f"Что-то сломалось: {exc}", ok=False)

        if not result.ok:
            # 👀 was a promise, and it was not kept. Taking it off is what
            # tells the user the difference between thinking and dead.
            for job in batch:
                await self._mark(job, False)

        if result.text:
            await self._deliver(last, result.text)
```

- [ ] **Step 4: The meta layer's public surface**

```python
# telegrind/meta/__init__.py — append to the docstring already there
import functools
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.meta.config import MetaConfig as MetaConfig
from telegrind.meta.queue import Job, Turns
from telegrind.meta.runtime import run_turn
from telegrind.meta.sessions import session_id as session_id
from telegrind.meta.sessions import turn_key

#: What the host has to supply. These four callables are the whole
#: coupling: nothing in this package imports the host's store, query or
#: taxonomy, so separating the module later is deleting one wiring file.
class Conversation(Protocol):
    """What the meta layer needs off the host's chat row, and no more."""

    id: int
    chat_id: int


ParentOf = Callable[[AsyncSession, int, int], Awaitable[int | None]]
SetReceipt = Callable[[Bot, AsyncSession, int, int, int, bool], Awaitable[None]]
Speak = Callable[[Bot, AsyncSession, int, str, int], Awaitable[None]]


class MetaLayer:
    """The conversation half, in one object.

    The Bot instance does not exist when the dispatcher is built, so the
    queue is constructed on the first hand-off, which is the first moment
    aiogram has handed us one.
    """

    def __init__(
        self,
        cfg: MetaConfig,
        *,
        async_session: async_sessionmaker[AsyncSession],
        parent_of: ParentOf,
        set_receipt: SetReceipt,
        speak: Speak,
    ) -> None:
        self._cfg = cfg
        self._async_session = async_session
        self._parent_of = parent_of
        self._set_receipt = set_receipt
        self._speak = speak
        self._turns: Turns | None = None

    def _queue(self, bot: Bot) -> Turns:
        if self._turns is None:
            self._turns = Turns(
                self._cfg,
                run=functools.partial(run_turn, self._cfg),
                deliver=functools.partial(self._deliver, bot),
                mark=functools.partial(self._mark, bot),
            )
        return self._turns

    async def _mark(self, bot: Bot, job: Job, started: bool) -> None:
        """A worker outlives the update that queued it, so it opens its own
        session rather than borrowing the handler's."""
        async with self._async_session() as session:
            await self._set_receipt(
                bot, session, job.chat_id, job.chat_pk, job.message_id, started
            )

    async def _deliver(self, bot: Bot, job: Job, text: str) -> None:
        async with self._async_session() as session:
            await self._speak(bot, session, job.chat_pk, text, job.message_id)

    async def hand_over(
        self,
        message: Message,
        row: object,
        chat: Conversation,
        session: AsyncSession,
        bot: Bot,
    ) -> bool:
        """Accept the message, or say plainly that we will not.

        False is not an error: it is «this chat is not on the allowlist»,
        and routing then answers the way it would if there were no meta
        layer at all. The gate is on the chat rather than on the verdict,
        so talk from a stranger is not answered.
        """
        # `row` is part of the HandOver signature and deliberately unused:
        # the meta layer routes on the chat, never on the verdict.
        chat_id, chat_pk = chat.chat_id, chat.id
        if not self._cfg.allows(chat_id):
            return False

        key = await turn_key(
            message,
            parent_of=functools.partial(self._parent_of, session, chat_pk),
        )
        await self._queue(bot).submit(
            Job(
                chat_id=chat_id,
                chat_pk=chat_pk,
                message_id=message.message_id,
                text=message.text or message.caption or "",
                key=key,
            )
        )
        return True
```

- [ ] **Step 5: The wiring file — the only place the two halves touch**

```python
# telegrind/bot/meta_wiring.py
"""Everything the meta layer needs from telegrind, and nothing more.

The module takes a config object and four callables; this file is those
callables. It exists so that separating the conversation half out later is
deleting one file rather than unpicking a merge — which is the whole reason
Claude is a handler and not a poller.
"""

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot import outbound
from telegrind.bot.handlers.receipts import HANDED_OVER, acknowledge, clear_receipt
from telegrind.models import Chat


async def parent_of(
    session: AsyncSession, chat_pk: int, message_id: int
) -> int | None:
    """What message N replied to, read off our own row.

    Telegram does not nest replies, so this is the only place the second
    hop of a session lookup can come from — and it is why the bot storing
    what it says had to land first.
    """
    row = await store.get_message(session, chat_pk, message_id)
    return store.reply_to(row) if row is not None else None


async def set_receipt(
    bot: Bot,
    session: AsyncSession,
    chat_id: int,
    chat_pk: int,
    message_id: int,
    started: bool,
) -> None:
    """👀 on when the turn starts, off when it failed.

    The row remembers it because there is no API to read a message's
    reactions back — and because `receipt_emoji == HANDED_OVER` is what
    makes the point of no return on an edit checkable without a second
    column.
    """
    emoji = HANDED_OVER if started else None
    async with session.begin():
        row = await store.get_message(session, chat_pk, message_id)
        if row is not None:
            row.receipt_emoji = emoji

    if emoji is not None:
        await acknowledge(bot, chat_id, message_id, emoji)
    else:
        await clear_receipt(bot, chat_id, message_id)


async def speak(
    bot: Bot, session: AsyncSession, chat_pk: int, text: str, reply_to: int
) -> None:
    """Claude always sends with reply parameters.

    Without them a reply to Claude has no parent row to point at, and every
    second turn would start a new conversation instead of continuing this
    one.
    """
    # In its own transaction: a bare get autobegins, and `say` opens one.
    async with session.begin():
        chat = await session.get(Chat, chat_pk)
    if chat is None:  # the row is created by the middleware before any turn
        return
    # 4096 characters is a hard Bot API limit and splitting is not the
    # answer — a long reply that does not fit a conversation leaves the
    # chat as an object. Until that exists, the truncation is visible.
    await outbound.say(bot, session, chat, text[:4096], reply_to=reply_to)
```

- [ ] **Step 6: Turn it on in `setup_dispatcher`**

```python
# telegrind/bot/setup.py
import logging

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.meta import MetaConfig, MetaLayer

from . import meta_wiring
from .dispatcher import dp
from .router import router

log = logging.getLogger(__name__)


def setup_dispatcher(
    async_session: async_sessionmaker[AsyncSession] | None = None,
) -> Dispatcher:
    """Import the handlers, which is what registers them.

    The meta layer is optional, and «off» is a supported state rather than
    a misconfiguration: without CLAUDE_ADMIN_CHAT_IDS the recording half
    runs exactly as before, talk is stored and left bare, and a refused
    question keeps saying so instead of going silent.

    There is no ChatActionMiddleware any more: it went with the echo, and
    nothing types.
    """
    from . import handlers, middleware  # noqa: F401

    cfg = MetaConfig.from_env()
    if cfg is None or async_session is None:
        log.info("meta layer off: CLAUDE_ADMIN_CHAT_IDS is not set")
    else:
        layer = MetaLayer(
            cfg,
            async_session=async_session,
            parent_of=meta_wiring.parent_of,
            set_receipt=meta_wiring.set_receipt,
            speak=meta_wiring.speak,
        )
        handlers.handlers.HAND_OVER = layer.hand_over
        log.info("meta layer on for %s chat(s)", len(cfg.admin_chat_ids))

    dp.include_router(router)

    return dp
```

```python
# main.py — pass the sessionmaker in, and build the dispatcher after it
async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    dp = setup_dispatcher(async_session)

    token = os.environ["BOT_TOKEN"]
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot, async_session=async_session)

    await engine.dispose()
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS.

- [ ] **Step 9: Walk it on the dev bot**

```bash
docker compose up -d postgres
uv run alembic upgrade head
CLAUDE_ADMIN_CHAT_IDS=<your chat id> CLAUDE_CWD=$PWD uv run python main.py
```

Check, in order:
1. `почему ты записал 4500 такси расходом` → 👀 appears within a second or two,
   then a short reply, sent as a reply to that message.
2. Reply to that reply with `а если бы я написал иначе` → 👀, then an answer
   that shows it remembers the first turn. This is the fork working.
3. Reply to *that* answer once more → still remembers. This is the third turn
   that resume-in-place could not have done.
4. Send two messages two seconds apart without replying → both get 👀 and one
   answer comes back on the second. This is the merge.
5. Reply to two *different* Claude messages in quick succession → two 👀 and two
   answers, neither waiting on the other.
6. `CLAUDE_TURN_TIMEOUT=2` and ask something slow → the 👀 comes off and the bot
   says it did not finish.
7. From a chat that is **not** in the allowlist, send `почему` → stored, no 👀,
   no reply.

- [ ] **Step 9: Lint, type-check, commit**

```bash
uv run ruff format && uv run ruff check && uv run ty check
git add -A && git commit -m "feat: the Claude handler — one turn per session, queued, with eyes

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Say what changed

**Files:**
- Modify: `.env.dist`, `docs/telegram-bot-api.md`, `CLAUDE.md`,
  `.claude/memory/project.md`
- Test: none — documentation.

- [ ] **Step 1: The new environment variables**

```ini
# .env.dist — append

# --- The Claude meta layer (optional) ---
# Comma-separated Telegram chat ids allowed to talk to Claude. UNSET MEANS
# OFF: the bot still records, classifies and reacts, and a question it
# cannot answer says so instead of being handed over. The gate is on the
# chat, not on the verdict — `talk` from a stranger is not answered at all.
CLAUDE_ADMIN_CHAT_IDS=
# Where the `claude` CLI lives, and what directory a turn runs in. On dev
# run the bot on the HOST (`uv run python main.py`), not in the container:
# the image has no claude binary and no ~/.claude.
CLAUDE_BIN=claude
CLAUDE_CWD=
# Seconds before a hung turn is given up on and said out loud.
CLAUDE_TURN_TIMEOUT=600
```

- [ ] **Step 2: The two Bot API facts this plan measured**

```markdown
<!-- docs/telegram-bot-api.md — under "Hard limits and gotchas" -->
- **`reply_to_message_id` is deprecated at 3.27** — the installed
  `methods/send_message.py` marks it `{"deprecated": True}`. Use
  `ReplyParameters(message_id=...)` on every send that is a reply.
- **Telegram does not nest replies.**
  `message.reply_to_message.reply_to_message` is always `None`; the API
  includes one level only. Anything that needs the grandparent has to look
  it up in our own `message` rows — which is one of the two reasons the bot
  stores what it says.
```

- [ ] **Step 3: The architecture section**

Update `CLAUDE.md`'s `## Architecture` request-flow diagram to the spec's:

```
Telegram message
  → Dispatcher (aiogram)
  → populate_chat_data middleware   # injects: session, chat, config
  → handlers/handlers.py            # store.upsert_message, COMMIT
  → classify.verdict_for            # one cheap call, after the commit
  → bot/routing.py
      fact     → 💔, enters the extraction tail
      question → answer.spec_for → SQL → text; refused → hand to Claude
      talk     → meta layer: queued, 👀 when the turn starts
```

Add `telegrind/classify.py`, `telegrind/bot/outbound.py`,
`telegrind/bot/routing.py` and `telegrind/meta/` to the **Key layers** list, one
paragraph each in the register of the ones already there. State in the deploy
section that the meta layer is **dev-only for now** and needs the bot run on the
host.

- [ ] **Step 4: The project memory**

Append to `.claude/memory/project.md` under a new
`## The Claude meta layer` heading:

- The dev bot must run on the **host** (`uv run python main.py`) for the meta
  layer; `telegrind-bot:dev` has no `claude` binary and no `~/.claude`.
- `--fork-session` is load-bearing at step 4, not step 5: a derived session id
  without it breaks on the third turn of a thread (2026-09-12).
- A cold `claude -p` turn cost $0.21 / 20 046 cache-creation tokens on `g15`,
  2026-09-12 — the number the warm base exists to remove.
- The spec's *Liveness* section is superseded by "Claude is a handler"
  (2026-09-12) and is deliberately not implemented.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs: the meta layer's config, its two Bot API facts, and what it changed

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
