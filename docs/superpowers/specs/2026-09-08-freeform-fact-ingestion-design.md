# Freeform fact ingestion — design

**Date:** 2026-09-08
**Status:** approved, pending implementation plan
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
`currency`, `date`. Column order in the cell is column order in the sheet, after a
`#` key column in A.

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
    message_id: int            # FK → message.id, ON DELETE CASCADE
    seq: int                   # 1-based within the message
    category: str
    fields: dict               # JSONB, {header: coerced value}
    model: str                 # which model produced this
    prompt_version: str        # which extraction prompt produced this
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
| `telegrind/sheets.py` | keep `Sheet`, `Config`, `ConfigSheet`, and generic row I/O; **delete** every `pattern`, `parse`, `make_row`, and the `Outcome`/`Loan`/`Wish` subclasses |
| `telegrind/services/expense.py` | **delete** |
| `telegrind/bot/handlers/handlers.py` | rewrite to the seven handlers above |

Deleting the regex path is not optional. Two parsers with different opinions about
the same message is worse than either alone.

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
   seven handlers, `/rebuild` and `/reload`, and the deletion of the regex path.
   At the end of this phase freeform text works end to end and the workbook is
   rebuildable.
2. **Re-extraction and rollups** — `/reparse`, the `??` escalation reply, Batch API
   submission, and `rollups.py` with its two templates. Depends on `prompt_version`
   and `model` already being recorded by Phase 1, so nothing is retrofitted.
3. **Voice** — `transcribe.py`, the `F.voice` handler, `/retranscribe`, and the
   transcript columns. The `Message` columns for it land in Phase 1's migration so
   this phase adds no migration of its own.

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

## Open questions

None blocking. Two probes gate implementation:

1. Structured outputs with `anyOf` + `const` discriminator on the chosen model.
2. `faster-whisper` latency and RSS for a ~10s Russian clip on the homeserver.
