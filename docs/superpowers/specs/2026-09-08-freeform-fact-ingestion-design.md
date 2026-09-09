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

**Caching.** The registry is loaded once per update in `populate_chat_data` and
injected into handlers, with an in-process TTL cache (60s) keyed by `sheet_url`.
`/reload` clears it. Without this, N categories would mean N `_config` reads and N
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

**Future facts need a consumer, not a mechanism.** Recording one is free today;
nothing reads it. Phase 6 below ("schedules derived from facts") is exactly that
consumer: a fact whose date is still ahead is what generates its own reminder.
Until then a future-dated expense is a note to self that projects and rebuilds
correctly and does nothing else. That is the honest state, and it is why nothing is
being built for it now.

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
| `/rebuild` | re-project the workbook from `fact` rows. No LLM cost. |
| `/reparse [--since D] [--category C] [--model M] [--stale]` | re-extract stored messages via the Batch API. Shows a token estimate and confirms before spending. `--category` selects messages whose *current* facts include that category; `--stale` selects those whose `prompt_version` is not the current one. |
| `/retranscribe [--since D]` | re-run whisper over stored voice messages, then re-extract them. |
| `/reload` | drop the cached `_categories` read. |
| reply `??` | re-extract just that one message with `LLM_MODEL_ESCALATE`, replace its facts, re-project, echo the new record. |

## Handlers

Registration order matters — aiogram stops at the first match.

1. `/start` FSM (unchanged)
2. `/rebuild`, `/reparse`, `/retranscribe`, `/reload`
3. reply `-` → delete
4. reply `??` → escalated re-extract
5. `F.voice` → transcribe → ingest
6. `F.text` → ingest
7. `edited_message` → re-ingest and diff

`populate_chat_data` gains `registry` and `config` alongside today's `session`,
`chat`, and `agc`.

**The `sheet_url` gate moves.** All five handlers today open with
`if not chat.sheet_url: set_state(request_sheet_url)` and return, which drops the
message. With Postgres as the source of truth the right order is: **write the
`Message` row first, then gate on `sheet_url` before projection.** A fact recorded
before onboarding finishes is recoverable by `/rebuild`; one dropped at the handler
is gone — and "nothing you write is ever lost" is the property this design is
claiming.

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

Kept because rewriting them buys nothing: `main.py`'s engine and credential wiring,
`middleware.populate_chat_data`'s shape, and the `/start` FSM in
`handlers/start.py`. Onboarding works and users depend on it.

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

## Roadmap — states and periods (Phase 4, not in scope here)

Two messages, sent at different times, describing one interval:

```
Tue: заболел
Fri: выздоровел          → болезнь: 08.09 – 11.09, 3 дня
```

```
01.08: бросил курить
(nothing yet)            → не курю: с 01.08, 39 дней и идёт
```

Habit tracking is the same shape as the sickness case, and the open-ended one is
the point of it: the answer is "how long so far", recomputed on every read.

### Why it does not fit as a category

Everything in this spec is a fact about one message. A period spans two, and a
naive implementation gives one row two owners — which breaks `sheet_key`
(`<message_id>_<seq>`), `facts_for_message`, and all four rows of the edit-diff
table at once. That is too much blast radius for a feature this small.

### The design: boundary events are facts, periods are a second projection

**One message still produces one fact and owns one row.** The fact is a *boundary
event*, not a period. Pairing happens in projection, where a fold over that
worksheet's events — ordered by time, grouped by subject — writes a derived
`<Worksheet>_periods` sheet. Nothing above this line changes: no new diff cases, no
shared row ownership, no schema change beyond what `fields` already holds.

Two column types carry it:

- **`state`** — what the period is about (`болезнь`, `не курю`). The grouping key.
- **`boundary`** — `начало` or `конец`.

A category declaring both *is* an interval category; the periods derivation is
triggered by the types, not by an extra registry cell. That is the same grain as
the rest of the registry, where types drive behavior.

The seeded category — a Phase 4 decision, **not** a change to Phase 1's
`SEED_CATEGORIES`:

| name | worksheet | when to use | columns |
|---|---|---|---|
| `state` | `States` | начало или конец состояния, привычки, периода | `Состояние:state, Событие:boundary, Время:date, Комментарий:text` |

Re-opening needs no special case: open/close/open/close over one subject folds into
two periods. The ongoing period's duration is written as a live `=NOW()-<start>`
formula rather than a computed number, so it keeps ticking while the bot is down —
the same reasoning that puts rollups in native formulas.

### Closing a period months later is the hard part

The fold groups by subject string, so `выздоровел` in November must produce the
same `Состояние` the September `заболел` did. Free text will not do that on its own.

**The currently-open subjects go into the user message**, so the model closes an
existing state by name instead of inventing a near-miss. It cannot go in the system
prompt: that prefix is cached, and per-chat state there would invalidate the cache
on every message — the same constraint that already puts the timestamp in the user
message.

For `/reparse` to stay reproducible, the injected list is reconstructed **as of the
message's own timestamp** from the fact table, not as of now. And the read is
skipped entirely for a workbook with no interval category, so the feature costs
nothing on the ingest path until it is used.

**Known risk, named because it will produce wrong data quietly: polarity.** The two
examples above open on opposite events — sickness opens on the bad one, abstinence
on the good one. So `покурил` is as plausibly the *opening* of a `курение` period as
the *closing* of a `не курю` one. One lived state, two namings, and the fold simply
never pairs them. The open-subject injection is the main mitigation; `??`
re-extraction and the edit path are the manual one. This is the part to watch in
testing, not the fold.

### Degenerate folds are non-fatal

A `конец` with no matching `начало`, and a `начало` with no `конец` — the second
being the whole point of the habit case. Both are reported on the periods sheet as
what they are, never dropped and never an error: a malformed pairing must not cost
a message, for the same reason a malformed registry row does not.

### Interaction with `/rebuild`

A periods sheet is a **third kind of sheet**: bot-owned and wholly rewritten, unlike
a data sheet (declared column range only) and unlike a `_summary` sheet (formulas
scaffolded once). `/rebuild` must rewrite it, and must **exclude it from the
unaccounted-keys guard** — its rows are folds, not facts, so every key there is
unaccounted for by construction and the guard would refuse every rebuild.

### Reading it back

`/state` lists open periods with their durations. That is the actual answer to "how
long have I not smoked", and it is cheap: the fold already exists.

**Depends on date resolution**, which is why the fixtures above matter here and not
only for expenses — `заболел в пятницу` is a past-dated boundary, and a boundary
placed on the wrong day silently mis-measures the period it opens.

**This is not reminders.** A period is derived and immutable, recomputable from its
events; an occurrence is mutable per-occurrence state. Phases 5 and 6 stay a
separate axis, and neither depends on this one.

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
