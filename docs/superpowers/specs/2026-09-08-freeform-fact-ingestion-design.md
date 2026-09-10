# Freeform fact ingestion — design

**Date:** 2026-09-08
**Status:** approved; Phase 1 planned in `docs/superpowers/plans/2026-09-09-freeform-facts-phase-1.md`
**Branch:** `freeform-facts`

## Problem

Every record today must be typed in a shape a regex accepts. `Outcome` wants a
leading number, `Loan` wants `долг <кто> <сумма>`, `Wish` wants `хочу …`. Anything
else hits `TIP_TEXT` and is lost. An LLM path exists but reaches exactly one
category: `record_outcome_llm` gates on `ExpenseService.is_expense` (`^\d+\b`), so
freeform text still dies unless it starts with a digit, and loans and wishes are
matched by regex before the LLM is ever consulted.

## Goal

Write any fact in any phrasing, by text or by voice. The bot decides which
category it belongs to, extracts that category's fields, and records it. Adding a
new category — telemetry, contacts, whatever comes next — is a spreadsheet edit,
not a deploy.

## Architecture

Postgres is the source of truth. The Google Sheets workbook is a **projection**
of it, rebuildable at any time.

```
Telegram message (text or voice)
  ↓
message table          — the immutable log: raw text or transcript, kept forever
  ↓  extraction (LLM, costs tokens)
fact table             — category + typed fields + which model produced them
  ↓  projection (Sheets API only, no LLM)
Google Sheets workbook
```

The two arrows have very different costs, which is the point of separating them:

- **Re-project** (`/rebuild`) rewrites the workbook from `fact` rows. No LLM cost.
  Run it after changing a category's columns, renaming a worksheet, or wiping a
  sheet by accident.
- **Re-extract** (`/reparse`, or `??` on one message) re-runs the model over stored
  `message` rows and replaces their facts. This is what makes "I don't like how it
  parsed that" recoverable — including with a stronger model than ran the first
  time. It costs tokens over real history, so bulk runs go through the Batch API at
  50% cost and confirm an estimate first.

Because the log is stored, escalation never asks the user to retype anything.

## The category registry

A `_categories` worksheet alongside today's `_config`. One row per category.

| name | worksheet | when to use | columns | rollup |
|---|---|---|---|---|
| `expense` | `Expenses` | потраченная сумма | `Сумма:money, Валюта:currency, Дата:date, Комментарий:text` | `sum_by_period(Дата, Сумма)` |
| `loan` | `Loans` | деньги в долг или возврат долга | `Сумма:money, Валюта:currency, Заёмщик:text, Дата:date, Комментарий:text` | `balance(Заёмщик, Сумма)` |
| `telemetry` | `Telemetry` | измерение о себе | `Метрика:text, Значение:number, Ед:text, Дата:date` | |
| `wish` | `Wishlist` | чего хочется | `Желание:text, Добавлено:date, Исполнено:text` | |
| `facts` | `Facts` | всё остальное — сохранить как есть | `Текст:text, Дата:date` | |

**Column headers are the field names.** There is no logical-name↔display-name
mapping layer: the header is what the LLM is asked for, what `fields` is keyed by,
and what a `rollup` argument refers to. One name, one place to change it.

**Types:** `text` (the default when `:type` is omitted), `number`, `money`,
`currency`, `date`, `due`. Column order in the cell is column order in the sheet,
after a `#` key column in A.

`date` and `due` differ only in which way they resolve an ambiguous reference:
`date` leans past ("во вторник" = the Tuesday just gone), `due` leans future ("во
вторник" = the Tuesday coming). Past-vs-future is a property of the column, not a
global rule of the prompt — today's `EXTRACT_INSTRUCTION` hardcodes *"assume they
are in the past"*, which would mis-parse every future-dated field the roadmap's
reminders need. `due` costs one line now and prevents re-touching the extraction
prompt later.

**Seeding.** On first read, if `_categories` is absent the bot writes the five rows
above. `expense`, `loan`, and `wish` reproduce today's worksheets and headers
exactly, so existing data keeps working with no migration; `telemetry` and `facts`
are new. Worksheets are still created lazily on first write (`Sheet.get_agw`), so a
seeded category you never use adds no clutter — no empty `Telemetry` sheet appears
until the first telemetry fact is recorded.

**`facts` is the required fallback** and is always present. Anything the model
cannot confidently place lands there as timestamp + raw text, so nothing written is
ever lost, and reading that sheet tells you which category to add next.

**Malformed rows are skipped, not fatal.** An unknown type, a duplicate `name`, a
`rollup` naming a column that does not exist — each is collected as a validation
error, the row is dropped, and the bot reports which row and why, once per registry
load. A broken registry row must never cost you a message. (Contrast
`ConfigSheet.get_data`, which does a bare `rows[i][1]` and survives only because
`_config` is bot-created and 2×2; a user-editable registry has a much wider blast
radius.)

**Widening a category can collide with a column you own, and is refused rather
than written.** Every write is positional: projection puts `len(headers)` values at
`A<row>` and clears `data_range(len(headers))`. So on a workbook where the bot
declares 5 columns and the sheet has 11, adding a sixth column in `_categories`
retargets the write onto the seventh — a formula column, filled down every row.
`check_headers` therefore requires the declared headers to be a **prefix** of the
worksheet's real header row: an empty cell is unclaimed and fine to write into, but
a differing non-empty header raises and nothing is written. `/rebuild` reports it
per worksheet and still projects the others, `/import` skips that sheet (`map_row`
reads by header, so importing it would mint facts with the wrong fields), and
ingest answers with the collision — the message row is already committed, so
`/rebuild` restores the fact once the header is fixed. To widen such a category,
move your own column to the right of the new range first.

The one case row 1 cannot reveal is a user column carrying formulas but no header.

**Caching.** The registry is loaded once per update in `populate_chat_data` and
injected into handlers, with an in-process TTL cache (60s) keyed by the **chat**,
not by `sheet_url`: a chat with no workbook still has a registry, and keying on
the URL made `invalidate(chat.sheet_url)` mean "every chat" the moment the URL was
null. `/reload` clears it. Without this, N categories would mean N `_config` reads and N
`find()` calls per message — today's three-sheet edit loop already does three
separate `_config` reads because `Transaction.__init__` builds a fresh
`ConfigSheet` per instance.

## Extraction

Direct `anthropic` SDK. Marvin is removed — it was there to evaluate marvin, and
its static-`target=Expense` requirement is exactly what a user-editable registry
cannot supply.

**Structured outputs** (`output_config={"format": <JSON Schema>}`) take the schema
as *data*, so it is built at runtime from the registry. No runtime pydantic model
generation, no untyped string envelope:

```
{"facts": [ anyOf: one object per category, discriminated by {"category": {"const": …}} ]}
```

Each branch declares that category's real fields with real types. One call returns
zero or more facts, which is what many-facts-per-message needs.

**Probe before building on this:** confirm structured outputs accepts `anyOf` with
a `const` discriminator on the chosen model. If it does not, the fallback is a
single flat object with `category` as an enum plus a `fields` string map, coerced
in code — still one call, still multi-fact, but no per-category type enforcement.

**Request shape:**

- Stable prefix — schema, rendered registry, extraction rules — carries
  `cache_control`.
- **The current timestamp goes in the user message, never the system prompt.**
  `EXTRACT_INSTRUCTION` interpolates `now` into the instructions today; doing that
  in a cached prefix invalidates the cache on every single message.
- No `thinking` parameter. Extraction does not need it, and the currently
  configured model (`claude-haiku-4-5`) is the `budget_tokens` generation where
  adaptive thinking is not available — the modern default would 400.
- `max_tokens` ~2048, enough for several facts.

**Model is configuration, not a constant.** `LLM_MODEL` for the normal path,
`LLM_MODEL_ESCALATE` for re-extraction. Both are read per call and recorded on each
fact row, so which model produced a given record is always answerable, and a model
change is a config change. Final model choice is deliberately deferred.

**Sign convention for loans moves into the prompt.** `Loan.make_row` encodes
`-100` and `100` both meaning lent, `+100` meaning repaid. The extraction rules
must state this explicitly — a bare `100` is not the sign a model would choose on
its own.

**Coercion** turns model output into sheet values: `money`/`number` normalize
`,`→`.` and strip currency glyphs; `currency` upper-cases, validates against
`pydantic_extra_types` `Currency`, and falls back to `_config`'s currency when
absent; `date` parses ISO first, then `dateparser` with the chat timezone, then the
message's own timestamp. Writes stay `ValueInputOption.USER_ENTERED` so numbers and
dates land typed in Sheets. `dateparser` is kept for this fallback; `marvin` is
dropped; `anthropic` is added.

### Past- and future-dated facts

**Both already work, and no new column type is needed for either.** Worth writing
down because the mechanism is not obvious and the natural instinct — add a
`past`/`future` flag — would be wrong.

The model receives the current timestamp in the user message and returns a
resolved ISO date-time. `date` tells it that an *ambiguous* reference leans past
("в понедельник" = the Monday just gone); `due` that it leans future. But an
ambiguous reference is the only thing that bias governs. **The message's own tense
overrides it**, and an explicit date is not ambiguous at all:

| message (now = Wed 2026-09-09) | resolved `Дата` |
|---|---|
| `41 бат массаж вчера вечером` | 2026-09-08 |
| `потратил тысячу на продукты в понедельник` | 2026-09-07 |
| `в пятницу заплачу 5000 за интернет` | 2026-09-**11** |
| `12 октября куплю подарок за 20000` | 2026-10-12 |

All four are `llm`-marked fixtures in `tests/fixtures/extraction.yaml` that assert
the resolved day, not just the category — verified passing 2026-09-09 on
`claude-haiku-4-5`. The third row is the load-bearing one: `Дата` on `expense` is a
`date` column, so the past-leaning bias *did* apply, and the future tense still won.

`date` vs `due` therefore stays what the spec already said it was — a hint for
ambiguity and for the `dateparser` fallback — not a gate on which direction a fact
may point.

**A date the message mentions is not necessarily the date of the fact.**
`купил билеты на самолёт на 15 октября за 50000` dated the expense to 15 October,
and `билеты в Тбилиси 12.10 за 90000` to 12 October. The money moved today; October
is the flight. Two of six realistic messages landed a month forward, in the wrong
`sum_by_period` bucket — a silent misfiling, since the row looks perfectly
well-formed.

The fix is definitional, in the prompt (`PROMPT_VERSION` 2026-09-09.2): a `date`
column is **when the fact happened** — when the money moved, when the measurement
was taken — and a date mentioned *about* the subject is not that. It belongs in a
`due` column if the category has one, and otherwise stays in the text field, where
`билеты на самолёт на 15 октября` keeps it verbatim. `due` is correspondingly
redefined as *a date the fact points at*: a deadline, a due date, the date
something is booked for.

Both halves matter. Without the `date` half a booking date silently becomes the
spend date; without the `due` half there is nowhere legitimate for the booking date
to go, and the rule would just be asking the model to throw information away. It is
also the answer to a gap the probe exposed: `дал Саре 1000 до 20 сентября` keeps
"до 20 сентября" only as prose, because the seeded `loan` category declares no
`due` column. Adding `Срок:due` to it is a `_categories` edit, not a code change —
but see the header-collision guard before widening a category on a workbook that
has your own columns to the right of the declared range.

**Future-dated facts are not inert, and an aggregating category is where that
bites.** `в пятницу заплачу 5000 за интернет` stays an `expense` dated forward
(stable 3/3), so `sum_by_period` counts it in this month's total before the money
moves. Recording a future fact is free; *aggregating* one is not. Two consequences:

- An unrealized intention is not an expense. `12 октября куплю подарок за 20000`
  lands in `wish` — stable 3/3, and correct: nothing was spent. The eval fixture
  that asserted `expense` for it was a bad oracle and has been changed.
- A committed future payment is a real fact with nowhere good to live yet. It sits
  in `expense` and inflates the current period until Phase 6 ("schedules derived
  from facts") gives it a home. The clean resolution is a rollup that filters on
  `Дата <= today`, which is a formula change and belongs with the rollup work in
  Phase 2 — noted here so it is not rediscovered from a wrong monthly total.

## Voice

Claude's API takes no audio, so transcription is a separate step with a separate
provider: **local `faster-whisper`** in the bot container. No API key, no
per-minute cost, and the audio never leaves the homeserver — which is the right
default for a personal life-log.

- Voice note → Telegram file → `faster-whisper` → transcript → **the same
  extraction path as text.** There is no separate voice extraction.
- The transcript is stored on the `message` row. Re-extraction reuses it and never
  re-transcribes; `/retranscribe` redoes it deliberately.
- **The reply echoes the transcript** above the records. Without it, a
  mis-transcription is indistinguishable from a mis-extraction.
- Whisper is CPU-bound and would block the aiogram event loop, so it runs in a
  thread executor behind a semaphore of 1.
- Config: `WHISPER_MODEL` (default `small`), `WHISPER_DEVICE` (`cpu`/`cuda`),
  `WHISPER_COMPUTE_TYPE` (default `int8`), `WHISPER_LANGUAGE` (default `ru`).
- Telegram does not allow editing a voice message, so voice corrections come
  through the `??` re-extract reply, not the edit path.

**Probe before building on this:** measure `faster-whisper` latency and RSS for a
~10s Russian clip on the homeserver, to pick the model size and confirm the RAM
floor is acceptable in the prod container.

## Data model

```python
class Message(Base):           # the log — append on first sight, updated on edit
    id: int                    # PK
    chat_pk: int               # FK → chat.id
    message_id: int            # Telegram's, BigInteger
    kind: str                  # "text" | "voice"
    text: str | None           # Telegram text or caption
    transcript: str | None     # whisper output for voice
    transcript_model: str | None
    audio_file_id: str | None
    audio_duration: int | None
    tg_date: datetime
    edited_at: datetime | None
    raw: dict                  # JSONB, the full aiogram Message dump
    created_at: datetime
    # UNIQUE (chat_pk, message_id)
    # content property → transcript or text: what extraction reads

class Fact(Base):              # replaceable derivation of a Message
    id: int                    # PK
    message_id: int | None     # FK → message.id, ON DELETE CASCADE; NULL if imported
    seq: int                   # 1-based within the message
    category: str
    fields: dict               # JSONB, {header: coerced value}
    origin: str                # "extracted" | "imported"
    model: str | None          # which model produced this; NULL if imported
    prompt_version: str | None # which extraction prompt produced this
    extracted_at: datetime
    worksheet: str             # where it landed
    sheet_key: str             # what is in column A: "4821.2"
    # UNIQUE (message_id, seq)
```

`Chat` and `File` are unchanged. One Alembic migration adds both tables.

**Naming trap:** `Chat.chat_id` is the *Telegram* chat id, while `Chat.id` is the
surrogate PK. `Message.chat_pk` is therefore a foreign key to `Chat.id`, not to
`Chat.chat_id`, and is named `chat_pk` so the two can never be confused at a call
site. `Message.message_id` and the uniqueness constraint `(chat_pk, message_id)`
both refer to Telegram ids and need `BigInteger`, as `Chat.chat_id` already does.

`sheet_key` is always `<telegram message_id>.<seq>`, including for single-fact
messages — uniform, and a message can gain a second fact on a later edit.

**Row indices are never stored.** `delete_rows` shifts them. `worksheet` is stored
instead, so a single-row operation is one DB read plus one `find(sheet_key)` in one
known sheet rather than a scan across every category.

`prompt_version` on each fact row is what makes staleness answerable: after a
prompt change, the facts that predate it are exactly the ones `/reparse` should
target.

**Messages are the log; facts are replaceable.** A Telegram edit overwrites
`message.text`, bumps `edited_at`, re-extracts, and diffs. Message revision history
is out of scope — the previous text is not kept.

## Edit and delete

Freeform ingestion creates a case today's code cannot handle: editing
`4500 такси` into `вес 82.4` must **move** the row from `Expenses` to `Telemetry`.
`change_row` only rewrites in place.

**Edit** re-extracts and diffs the new facts against the stored ones by `seq`:

| condition | action |
|---|---|
| same `seq`, same category | rewrite the row in place |
| same `seq`, different category | delete from the old worksheet, append to the new, update the fact row |
| `seq` present before, absent now | delete the row and the fact |
| `seq` absent before, present now | append the row, insert the fact |

**Delete** (reply `-`) reads the replied message's facts, deletes each row in its
own worksheet, and deletes the fact rows. The message row stays — the log is not
rewritten by a delete.

**Fixes a live bug.** `delete_record` filters on `F.reply_to_message.text` and
falls off the end returning `None` when the text is not `-`. The handler matched,
so aiogram stops propagation: **every reply that is not `-` is silently swallowed
today.** Its filter becomes `F.reply_to_message & (F.text == "-")` so a reply
carrying a fact falls through and gets recorded.

## Reply format

Every write echoes the record — the transparency is worth the extra message, and it
makes the edit and delete affordances discoverable.

```
You: 4500 такси и вес 82.4
Bot: 2 записи:
     Expenses  · 4500 KZT · 08.09.26 21:40 · "такси"     ████ (4821.1)
     Telemetry · вес = 82.4 · 08.09.26 21:40             ████ (4821.2)
```

The `<key>@<worksheet>` pointer stays in a `tg-spoiler`, as
`ExpenseService.make_reply_text` does today. Voice replies prepend the transcript.

## Rollups

A category's `rollup` cell names a rollup the bot scaffolds **once**, as native
Sheets formulas. The sheet then recalculates itself on every new row, with no bot
involvement — it keeps working while the bot is down, and the formula is readable
and editable.

v1 templates:

- `balance(group_col, amount_col)` — net per counterparty, which is the "how much
  does someone owe me" case.
- `sum_by_period(date_col, amount_col)` — monthly totals.

An unrecognized function name is a registry validation error. New templates are a
code change, deliberately.

**Formulas go on a separate `<Worksheet>_summary` sheet, not into a fixed cell of
the data sheet.** `apply_filter` restricts itself to `"A:A"` specifically so
user-added columns are never touched; writing a rollup to `H1` would undo that.

**The ensure-step is explicit and idempotent, not creation-time.**
`Transaction.get_agw` writes headers only when `created` is true, so existing
`Expenses`/`Loans`/`Wishlist` sheets would never be scaffolded by a creation hook.
`ensure_rollup` checks for a marker before writing and is safe to call on every
write.

## Commands

| command | effect |
|---|---|
| `/link [url]` | attach a Google workbook, or report the attached one. With no argument and nothing attached, explains how to share one. |
| `/import [--dry-run]` | read the attached workbook's existing rows in as facts. Idempotent. |
| `/rebuild` | re-project the workbook from `fact` rows. No LLM cost. This *is* the export: on a freshly linked empty workbook it creates every declared worksheet and writes every fact, so no separate `/export` command exists. |
| `/reparse [--since D] [--category C] [--model M] [--stale]` | re-extract stored messages via the Batch API. Shows a token estimate and confirms before spending. `--category` selects messages whose *current* facts include that category; `--stale` selects those whose `prompt_version` is not the current one. |
| `/retranscribe [--since D]` | re-run whisper over stored voice messages, then re-extract them. |
| `/reload` | drop the cached `_categories` read. |
| reply `??` | re-extract just that one message with `LLM_MODEL_ESCALATE`, replace its facts, re-project, echo the new record. |

## Handlers

Registration order matters — aiogram stops at the first match.

1. `/start` — intro only; there is no FSM and nothing to configure
2. `/link`, `/import`, `/rebuild`, `/reparse`, `/retranscribe`, `/reload`
3. reply `-` → delete
4. reply `??` → escalated re-extract
5. `F.voice` → transcribe → ingest
6. `F.text` → ingest
7. `edited_message` → re-ingest and diff

`populate_chat_data` gains `registry` and `config` alongside today's `session`,
`chat`, and `agc`.

**The `sheet_url` gate is gone, and so is onboarding.** All five handlers today
open with `if not chat.sheet_url: set_state(request_sheet_url)` and return, which
drops the message. The first revision of this spec moved that gate down to just
before projection. That was still one step short: with Postgres as the source of
truth there is nothing left for the gate to protect, because **the workbook is not
a prerequisite for anything.**

`load_registry(None, …)` returns the seeded categories and `load_config(None, …)`
returns the defaults (+06:00, KZT), so `registry` and `config` are never null and
extraction runs with nothing linked. `apply_changes` and `delete_facts` do their
Postgres half unconditionally and their Sheets half only when there is a sheet. A
fact recorded with no workbook is projected by the first `/rebuild` after one is
attached — the same recovery path a fact recorded *before* projection already
used, so this adds a starting point rather than a failure mode.

The FSM and its `request_sheet_url` state are deleted. `/start` sends the intro
and the tips; attaching a workbook is `/link <url>`, a command the user reaches
for when they want a spreadsheet rather than a gate they must pass before the bot
will record anything.

### A leading amount is an expense, whatever follows it

`444 куколд` landed in `facts`. So did `444 кукольный`, `444 абракадабра` and a
bare `444`, while `444 тенге` was an expense and one run of `444 хуйня`
extracted nothing at all. It was never about the word: the model only read a
leading number as money when the trailing word named something it recognised as
buyable. That shape — an amount, then whatever — is what the regex bot matched
as an expense and the most common thing written to this bot, so the miss is
expensive.

The fix is one rule, and its hedge is load-bearing:

> When a message opens with an amount of money **and no other category fits
> it**, it is an `expense`, and the rest of the message is the comment.

Two stronger wordings were measured and both did damage. "Begins with a bare
number … and it is never `facts`" dragged `12 октября куплю подарок за 20000`
out of `wish` and into `facts` — told that a leading number must be an expense
and never `facts`, the model could not reconcile that with an intention being a
wish, and escaped to `facts` anyway. Adding an explicit carve-out for a leading
*date* made it worse, taking `в пятницу заплачу 5000 за интернет` down with it:
naming those cases inside the rule pulled them into its orbit. Leaving the
model room to prefer another category is what keeps them out of it.

### Why the bot cannot create the spreadsheet itself

The obvious version of this — the bot creates a fresh workbook and shares it —
does not exist. Measured 2026-09-09 against the production service account:

```
about.get → storageQuota { limit: "0", usage: "0" }
files.create (Google-native spreadsheet) → 403 storageQuotaExceeded
files.copy   (of an existing workbook)   → 403 storageQuotaExceeded
```

A service account outside a Workspace domain has **no Drive of its own**. It can
read and write any workbook shared with it, forever, but it can never own a file,
so it can never create one — and there is correspondingly nothing to transfer
ownership *from*. (The Drive API also has to be enabled in the GCP project at all;
before that the same calls fail `SERVICE_DISABLED`, which is a different error with
the same symptom.)

So `/link`'s help text asks the user to create an empty spreadsheet and share it
with the service account, and says why. The consolation is real: the user owns the
workbook, it sits in their own Drive rather than under "Shared with me", and they
can delete it — none of which would be true of a workbook the bot had created.

Bot-created workbooks would need OAuth as the user (the `drive.file` scope is
non-sensitive, so no Google verification and no expiring refresh token, but it can
only touch files the app itself created — so importing a pre-existing workbook has
to happen before any such switch). Roadmap, not Phase 1.

## Module layout

| module | responsibility |
|---|---|
| `telegrind/registry.py` | *new* — parse, validate, seed, and cache `_categories` |
| `telegrind/llm.py` | *new* — Anthropic client, JSON Schema builder, extraction call, prompt + `prompt_version` |
| `telegrind/coerce.py` | *new* — typed field coercion |
| `telegrind/rollups.py` | *new* — formula templates and the idempotent ensure-step |
| `telegrind/projection.py` | *new* — facts → sheet rows; upsert, delete, full rebuild |
| `telegrind/transcribe.py` | *new* — faster-whisper behind a thread executor |
| `telegrind/store.py` | *new* — message and fact repository functions |
| `telegrind/models.py` | add `Message` and `Fact` |
| `telegrind/sheets.py` | reduced to a **worksheet client** — get-or-create, append, find, update, delete, filter. No subclasses, no `pattern`, no `parse`, no `make_row` |
| `telegrind/services/expense.py` | **delete** |
| `telegrind/bot/handlers/handlers.py` | rewrite to the seven handlers above |

Deleting the regex path is not optional. Two parsers with different opinions about
the same message is worse than either alone.

### What "rewrite" does and does not include

The Python is not preserved out of seniority. The `Sheet` → `Transaction` →
`Outcome`/`Loan`/`Wish` hierarchy exists *because* categories used to be
compile-time; with a runtime registry a category is a row of data, not a class, so
the hierarchy has nothing left to express. `sheets.py` keeps only the parts that
are about talking to a worksheet — lazy get-or-create, the `A:A` basic filter, row
find/append/update/delete — and loses everything that was about knowing what a
message means.

Kept because rewriting them buys nothing: `main.py`'s engine and credential
wiring, and `middleware.populate_chat_data`'s shape.

The `/start` FSM in `handlers/start.py` is **not** kept. An earlier revision of
this spec kept it on the grounds that onboarding worked and users depended on it;
that was true of the regex bot, where the spreadsheet *was* the database. Once
Postgres holds the facts, the onboarding step's only remaining effect is to make
the bot's first answer a refusal.

**The data contracts are fixed, and this is the part "from scratch" does not
reach:**

- **The prod database.** `telegrind_pgdata` holds live `chat` rows with real
  `sheet_url` values. `Chat` and `File` keep their tables and both existing Alembic
  revisions; the new work is one additive migration on top.
- **The `_config` worksheet.** Its two Russian key labels are already sitting in
  real spreadsheets. The class may be rewritten; the sheet format may not change.
- **The existing worksheets.** `Expenses`, `Loans`, and `Wishlist` hold real
  history with real headers. The seeded registry reproduces them exactly — see the
  import step below, which is what makes that safe.

## Importing existing history

**Without this step, the first `/rebuild` destroys real data.** The design makes
the workbook a projection of `fact` rows, and `/rebuild` clears and rewrites each
category's declared column range. On day one the `fact` table is empty while the
workbook holds years of expenses — so a rebuild would write nothing over
everything.

A one-time import, run as part of Phase 1:

- Read every row of each seeded category's worksheet and synthesize a `fact` row
  per sheet row: `category` from the registry, `fields` keyed by the sheet's own
  headers, `sheet_key` from column A, `origin = "imported"`.
- Imported facts have **no `message`** — `Fact.message_id` becomes nullable. Their
  source text was never stored, so they are re-projectable but **not
  re-extractable**, and `/reparse` skips them. That is an honest limit, not a bug:
  the log starts the day the bot starts logging.
- Column A already holds the Telegram `message_id` for bot-written rows, so an
  imported fact keeps its identity and a later edit to that original message still
  finds its row.

**`/rebuild` refuses to run** on a worksheet holding rows whose keys are not
accounted for by facts, and says how many, unless given an explicit `--force`. A
projection that can silently discard its own source is not worth the convenience.

### Retiring the original workbook

The original spreadsheet is a hand-grown thing with formula columns, extra sheets
and years of drift. Once its rows are facts, none of that has to be carried
forward: a fresh workbook is a cleaner projection than the old one can ever be
edited into. The order is not negotiable, because until step 2 completes the old
workbook is the **only** copy of the pre-bot history:

1. Deploy. `chat.sheet_url` still points at the original workbook, and it is still
   honoured — that is what makes step 2 possible.
2. `/import`. Verify the counts against the sheets by hand.
3. Create an empty workbook, share it with the service account, `/link` it.
4. `/rebuild`. A brand-new workbook has no unaccounted keys, so this needs no
   `--force` — the guard is satisfied by construction rather than overridden.
5. Compare, then retire the original: unshare it from the service account so no
   future misconfiguration can reach it.

Facts carry their own `sheet_key`, synthesized `import-<worksheet>-<row>` keys
included, so step 4 reproduces every key exactly and stays idempotent. Nothing
about the projection is tied to the identity of the workbook it was last written
to.

One deploy hazard, unrelated to the workbook: `facts_for_message` filters on
`Fact.message_pk`, which is null for imported facts. Editing a message sent in the
window between the deploy and `/import` writes a second row instead of rewriting
the first. A prompt `/import` closes the window.

## Testing

`pytest` + `pytest-asyncio` as dev deps. These are pure functions and get real
unit tests, written first:

- registry parsing and validation, including every malformed-row case
- JSON Schema construction from a registry
- field coercion per type, including the date fallback chain
- the fact diff (all four rows of the edit table)
- rollup formula rendering
- projection row ordering and key format

Extraction quality is not unit-testable. It gets a fixture file of real messages →
expected category, run on demand, as the seed of a proper eval set later.

Sheets I/O, Telegram I/O, and whisper stay untested.

## Build order

Three independently shippable phases. Phase 1 is the whole point and stands alone;
2 and 3 are additive and touch nothing Phase 1 owns.

1. **Core ingestion** — registry, extraction, `Message`/`Fact`, projection, the
   import of existing worksheet history, the seven handlers, `/rebuild` and
   `/reload`, and the deletion of the regex path. At the end of this phase freeform
   text works end to end and the workbook is rebuildable without data loss.
2. **Re-extraction and rollups** — `/reparse`, the `??` escalation reply, Batch API
   submission, and `rollups.py` with its two templates. Depends on `prompt_version`
   and `model` already being recorded by Phase 1, so nothing is retrofitted.
3. **Voice** — `transcribe.py`, the `F.voice` handler, `/retranscribe`, and the
   transcript columns. The `Message` columns for it land in Phase 1's migration, so
   this phase adds no *schema* migration — but it is not self-contained on the
   deployment side. `faster-whisper` pulls CTranslate2 and a downloaded model, which
   changes the image and raises the prod container's RAM floor, and **the prod
   compose lives in the `vps` repo, not here.** Phase 3 therefore includes a `vps`
   change: a memory limit that fits the chosen model size, and a volume or bake-in
   for the whisper model cache so it is not re-downloaded on every `docker compose
   up -d --force-recreate`.

## Roadmap — subject-keyed aggregates: balances and periods (Phase 4, not in scope here)

Two requests that turn out to be one mechanism.

**A period spans two messages.**

```
Tue: заболел
Fri: выздоровел          → болезнь: 08.09 – 11.09, 3 дня
01.08: бросил курить
(nothing yet)            → не курю: с 01.08, 39 дней и идёт
```

**A loan balance spans many.**

```
дал Саре 1000
дал Саре ещё 1000
Сара вернула 100   (x8)  → Сара: должна 1200
```

Both are a **fold over a stream of facts, grouped by a subject column, read as
current state.** Neither is a new kind of record: the facts are already there, one
per message, and what is missing is the view. That distinguishes them from
`sum_by_period`, which groups by *time bucket* — a time-keyed fold needs nothing
from the subject, and is the reason to say two kinds rather than three templates of
one.

### `rollup` is the right home, and periods are a template in it

Last revision of this section called a periods sheet "a third kind of sheet". That
was wrong. The registry already has a `rollup` cell naming a template with column
arguments, rendered onto `<Worksheet>_summary` — which is exactly the interface a
fold needs:

| template | fold | rendered as |
|---|---|---|
| `balance(Заёмщик, Сумма)` | signed sum per subject | native formula (`SUMIF` per subject) |
| `periods(Состояние, Событие, Время)` | pair open/close per subject | bot-written rows |
| `sum_by_period(Дата, Сумма)` | sum per month | native formula |

The interface unifies; only the implementation splits, and one flag on the template
says which side it is on. Pairing consecutive rows per subject is miserable as a
formula, so `periods` is computed by the bot and rewritten on every write to that
worksheet and on `/rebuild`. The *open* period's duration is still written as a live
`=NOW()-<start>` formula rather than a number, so it keeps ticking while the bot is
down — the same reasoning that puts the other two in formulas.

### The load-bearing asymmetry: "closed" is derived for balances, declared for periods

This is what makes loans the easy case and periods the hard one, and it is worth
stating before either design:

- **A balance needs no closing event.** The sign carries the direction, the
  extraction prompt already states the convention, and "settled" is *derived* —
  the balance reached zero. Nothing has to be said, so nothing can be said wrong.
- **A period needs a declared close**, and the close arrives in a separate message
  possibly months later.

So the loan case needs no new machinery beyond the `balance` template that Phase 2
already owes. Its gap today is only that `balance` is unimplemented: the `Loans`
sheet holds correctly signed amounts and offers no aggregate view of them.

**Scope call: a net balance per counterparty, not per-loan lots.** Sarah's 100 is
not matched against loan #1 or loan #2. "How much does she owe me" is answered by
the net; FIFO lot matching is a different feature and not this one.

### Periods: two new column types, and the subject-naming problem

**One message still produces one fact and owns one row.** The fact is a *boundary
event*, not a period. That is the whole reason this design is cheap: nothing above
it changes — not `sheet_key` (`<message_id>_<seq>`), not `facts_for_message`, not
any of the four rows of the edit-diff table. A period spanning two messages would
otherwise give one row two owners and break all three at once.

- **`state`** — what the period is about (`болезнь`, `не курю`). The grouping key.
- **`boundary`** — `начало` or `конец`.

A category declaring both *is* an interval category, and gets the `periods`
template. Types drive behavior, as everywhere else in the registry. The seeded
category — a Phase 4 decision, **not** a change to Phase 1's `SEED_CATEGORIES`:

| name | worksheet | when to use | columns |
|---|---|---|---|
| `state` | `States` | начало или конец состояния, привычки, периода | `Состояние:state, Событие:boundary, Время:date, Комментарий:text` |

Re-opening needs no special case: open/close/open/close over one subject folds into
two periods.

**The subject string must survive months.** `выздоровел` in November has to produce
the same `Состояние` that `заболел` produced in September, and free text will not do
that unaided. **The currently-open subjects go into the user message**, so the model
closes an existing state by name rather than inventing a near-miss. Not the system
prompt: that prefix is cached, and per-chat state there would invalidate the cache
on every message — the same constraint that already puts the timestamp in the user
message. For `/reparse` to stay reproducible the list is reconstructed **as of the
message's own timestamp** from the fact table, not as of now; and the read is
skipped entirely for a workbook with no interval category, so the ingest path costs
nothing until the feature is used.

**Why that mitigation is the right one: look at what loans do differently.** A
counterparty is a name from a small recurring set, so the model reproduces it
without help. A state's subject is free phrasing, so it needs the set supplied.
Same fold, and the difference between them is precisely the stability of the
grouping key — which is the argument for injecting it.

**Known risk, named because it fails quietly: polarity.** The two examples open on
opposite events — sickness opens on the bad one, abstinence on the good one. So
`покурил` is as plausibly the *opening* of a `курение` period as the *closing* of a
`не курю` one. One lived state, two namings, and the fold never pairs them. Loans
have no equivalent exposure: a sign is unambiguous where a boundary label is not.
Watch this in testing rather than the fold itself; `??` re-extraction and the edit
path are the manual repair.

### Degenerate folds are non-fatal

A `конец` with no matching `начало`, and a `начало` with no `конец` — the second
being the whole point of the habit case. A balance is never degenerate, which is
another way of saying the same asymmetry. Both period cases are reported on the
summary sheet as what they are, never dropped and never an error: a malformed
pairing must not cost a message, for the same reason a malformed registry row does
not.

### Interaction with `/rebuild`

A bot-written `periods` sheet must be rewritten by `/rebuild` and **excluded from
the unaccounted-keys guard** — its rows are folds, not facts, so every key there is
unaccounted for by construction and the guard would refuse every rebuild. Formula
rollups do not have this problem, which is exactly why the flag distinguishing them
is not cosmetic.

### Reading it back

`/state` lists open periods with their durations — the actual answer to "сколько я
не курю", and cheap once the fold exists. Balances need no command: the
`<Worksheet>_summary` sheet already shows them, and there is no open-period
equivalent that has no natural row.

**Depends on date resolution**, which is why the fixtures above matter here and not
only for expenses: `заболел в пятницу` is a past-dated boundary, and a boundary on
the wrong day silently mis-measures the period it opens.

**Neither of these is reminders.** A balance and a period are derived and
recomputable from their facts; an occurrence is mutable per-occurrence state. Phases
5 and 6 stay a separate axis, and neither depends on this one.

## Roadmap — reminders (Phases 5 and 6, not in scope here)

Recorded so the phases above do not have to be re-opened for it. Everything in this
spec is a **fact about the past**: immutable, derived once, projected. A reminder is
an **obligation about the future** — mutable per-occurrence state, and the bot
speaking first, which it does nowhere today. So it is a second axis, not a sixth
category.

What it will need, none of which exists yet:

- **A clock.** A `due_at` column polled by an asyncio task, backed by Postgres —
  not an in-process scheduler. Pushing to `main` recreates the container, so
  in-memory schedules would vanish on every deploy.
- **Recurrence.** `dateutil.rrule` covers "monthly" and "twice a day".
  `python-dateutil` already arrives as a `dateparser` dependency.
- **Occurrence rows.** Fired / acknowledged / missed / snoozed is per-occurrence
  mutable state, which is precisely what a `Fact` is not.

Two mechanisms, in order:

- **Phase 5 — declared schedules.** A `remind(...)` column in `_categories`,
  sitting beside `rollup`, so a category carries scheduling metadata the same way
  it carries rollup metadata. This covers a pills schedule.
- **Phase 6 — schedules derived from facts.** A loan with a `due`-typed column
  generates its own payback reminder; a subscription expense recurring monthly
  generates its own. Nothing is typed twice. Strictly more powerful, and it is what
  makes reminders read as the same system rather than a bolted-on todo list. It
  depends on Phase 5's occurrence machinery.

The only forward-compatibility cost paid now is the `due` column type, so the
extraction prompt never has to be re-touched for this.

## Non-goals

- Multi-currency netting or FX conversion in rollups.
- Message revision history.
- **Reading hand edits back from Sheets.** The projection is one-way. A value you
  type into a bot-written cell is overwritten on the next `/rebuild`. Columns you
  add yourself are safe: `/rebuild` clears and rewrites only the declared column
  range of a category, never the whole worksheet.
- Multi-user chats. One chat, one workbook, one person.
- Proposing new categories from clustered `Facts` rows. Attractive, but it belongs
  after the ingestion path is proven.

## Probes

Not open questions — **prerequisites**. Each is the first task of the phase it
gates, and its outcome can still change that phase's shape.

1. **Gates Phase 1 — PASSED 2026-09-09** on `claude-haiku-4-5`,
   `anthropic` 0.97.0. `anyOf` *inside an array's `items`* is accepted, so the
   discriminated-union schema in this spec is the schema, and the flat `category`
   enum + string-map fallback is not needed. What the probe settled:

   - Call shape: `messages.create(..., output_config={"format": {"type":
     "json_schema", "schema": <dict>}})`, then `json.loads(resp.content[0].text)`.
     Not `messages.parse`, which wants a static type. `stop_reason` was `end_turn`,
     never `refusal`.
   - Cyrillic property names (`Сумма`, `Заёмщик`) work verbatim, so column headers
     really can be the field names.
   - `{"type": "string", "format": "date-time"}` is honored — the model returned
     `2026-09-09T21:40:00+06:00`, offset included, from the timestamp in the user
     message. No invented format.
   - **A partial `required` list is accepted**, so a column can be optional. The key
     is then *absent* from the object rather than empty — coercion must handle a
     missing key, not just an empty string.
   - `additionalProperties: false` on every branch is accepted.
   - Multi-fact works: `4500 такси и вес 82.4` returned one `expense` and one
     `telemetry` in one call. `позвонил маме` fell to `facts` unprompted.
   - **`cache_control` silently no-ops below the model's minimum cacheable prefix.**
     At 1047 input tokens `cache_creation_input_tokens` was 0. Keep the breakpoint —
     it costs nothing and starts working as the registry grows — but never assert a
     cache hit in a test.
2. **Gates Phase 3.** `faster-whisper` latency and resident memory for a ~10s
   Russian clip on the homeserver, which picks the model size and confirms the RAM
   floor is acceptable in the prod container.
