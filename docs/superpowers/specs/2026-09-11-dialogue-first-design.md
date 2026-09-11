# Dialogue-first telegrind — design

Status: approved in outline 2026-09-11, pending review of this document.
Supersedes the workbook-centred parts of
`2026-09-08-freeform-fact-ingestion-design.md`. Phase 1 of that design
(`Message`/`Fact` rows, `llm.extract`, `projection.apply_changes`) shipped in
`ef80f31`; this document keeps its ingest half and retires its projection half.

## Problem

The workbook is the product surface today: extraction runs on every incoming
message so the sheet stays live, and the bot's reply is an echo of the row it
just wrote. Three things are wrong with that.

**Per-message extraction throws away context.** `и молоко 300`, `ещё 5000`,
`это было вчера` only mean something next to their neighbours. One LLM call per
message cannot see them.

**The schema has to be declared in advance.** `_categories` is a worksheet the
user edits, `Category.worksheet` and `Column.header` are spreadsheet
coordinates, and a category that is not declared cannot be recorded. Expenses,
measurements, habits, debts, assets — the list does not close.

**The spreadsheet is the wrong product.** What the user wants is to write things
down and later ask about them. The sheet is one way to look at the answer, not
the answer.

## Goal

A bot you write into and ask questions of. Nothing is parsed until a question
makes parsing necessary. The shape of the data is discovered from what has been
written, not declared ahead of it. Arithmetic stays in SQL.

The workbook, if it comes back, is an addition to the bot, not its storage.

## Architecture

```
any message      → store, unconditionally. Nothing else happens.
/q <question>    → extract the unparsed tail → query spec → SQL → answer
edit / delete    → re-extract that one message
```

**Asking is the trigger.** `/q` is the only reason extraction has to have
happened, so it is the only thing that causes it. Deferred parsing and the
dialogue product are one mechanism, not two features that coexist. There is no
second trigger to design — a background flush on a timer stays a knob the
extraction-state column makes available, and it is not in this scope.

**The echo is gone.** Writing a message produces no reply. This is a deliberate
loss: the confirmation that the bot understood now arrives when you ask, in the
answer. See *Acknowledgement and errors* for what replaces it.

## Data model

### `message` — the log, unchanged in shape

Keeps `chat_pk`, `message_id`, `kind`, `text`, `transcript`, `audio_file_id`,
`tg_date`, `edited_at`, `raw`, `created_at`, and the
`(chat_pk, message_id)` uniqueness. `store.upsert_message` is kept as is.

Five columns are added:

| Column | Why |
| --- | --- |
| `extracted_at timestamptz \| null` | The marker that makes the batch pass idempotent. |
| `extract_model text \| null` | Which model produced this message's facts. |
| `extract_prompt_version text \| null` | Which prompt did. Together with the model, what a re-extraction decision keys on. |
| `extractable bool not null default true` | False for `/q`, for commands, and for imported bot replies. |
| `extract_error text \| null` | The last extraction failure for this message, so a failure is reportable rather than silent. Cleared on success. |

`extracted_at` is not derivable from "has `fact` rows". Deferred extraction
makes *not yet parsed* and *parsed, yielded nothing* (`привет`, `ок`, `??`)
indistinguishable without it, and every pass would re-feed the no-ops forever.
It is set on every message in the window, including those that produced nothing.

`extractable = false` matters more than it looks. A `/q` message is stored like
everything else — the invariant holds, nothing written is ever lost — but it
must never reach the extractor, or the batch pass coins a `kind` out of
«сколько я потратил на еду» and poisons the observed taxonomy that the whole
design depends on.

### `fact` — service columns plus JSONB

```
id            bigserial primary key
chat_pk       fk chat.id on delete cascade
message_pk    fk message.id on delete cascade, not null
seq           int not null             -- 1-based position within the message
kind          text not null            -- free-form, coined by the model
at            timestamptz not null     -- when the fact HAPPENED
fields        jsonb not null           -- everything else
model         text
prompt_version text
created_at    timestamptz not null default now()
updated_at    timestamptz not null default now()
deleted_at    timestamptz null

unique (message_pk, seq) where deleted_at is null
index  (chat_pk, kind, at) where deleted_at is null
index  gin (fields)
```

Dropped from the current table: `worksheet`, `sheet_key`, `origin`. `category`
becomes `kind`.

`message_pk` becomes NOT NULL. It was nullable only because an imported
spreadsheet row had no message behind it; history now imports as messages, so
every fact has a source. `at` is NOT NULL too, falling back to the message's
`tg_date` when the text states no time of its own.

The uniqueness is **partial** — `where deleted_at is null`. A tombstoned fact
keeps its `(message_pk, seq)`, so a total constraint would make re-extracting
an edited message collide with the row it is replacing.

Three promotions out of JSONB, and no more:

- **`kind`** — every query filters on it, and it is the axis the observed
  taxonomy is computed along.
- **`at`** — *when the fact happened*, which is not `created_at`. «вчера
  потратил 5000» is created today and happened yesterday. Every question has a
  period in it. JSONB has no timestamp type, so leaving it there means a string
  in whatever format the model felt like; the coercion has to happen somewhere,
  so it happens once, on write.
- **`deleted_at`** — a soft delete, and not for undo. Facts are derived and
  re-derivable: a hard delete is undone by the next re-extraction of the same
  message (an edit, a prompt-version bump). The tombstone is what makes a
  deletion stick.

**The numeric shape stays in JSONB on purpose.** Expenses are flows
(`amount` + `currency`), measurements are levels (`value` + `unit`), habits have
no number at all, assets have a balance rather than a flow. Committing to one of
those now is the least reversible decision available, and there is no evidence
yet for which one wins. Promotion later is a generated column —
`GENERATED ALWAYS AS ((fields->>'amount')::numeric) STORED` — not a rewrite of
the write path.

### Numbers must be real JSON numbers

The price of deferring the numeric decision. `(fields->>'amount')::numeric`
does not fail one row, it fails the whole query, and adding a generated column
later fails on the first non-numeric value in the table.

So coercion happens at the write boundary, never at read: a value that parses
becomes a JSON number; a value that does not (`около 500`, `5 000`, `много`)
is stored as text under a *different* key, and the fact survives — it simply
does not aggregate. Nothing written is ever lost; that includes a number the
model could not pin down.

## Extraction

Triggered by `/q`, and by an edit of an already-extracted message.

**The window** is the unextracted tail plus a few already-extracted messages
carried in as read-only context. Default: the whole unextracted tail up to 200
messages per pass, plus the 10 messages before it. Context messages are shown to
the model and are not re-extracted.

**The prompt carries the observed taxonomy.** The registry is no longer
declared, it is observed:

```sql
select kind, jsonb_object_keys(fields) as field, count(*)
from fact where chat_pk = :chat and deleted_at is null
group by 1, 2 order by 3 desc
```

That listing goes into the prompt with one rule: reuse an existing `kind` and an
existing field name if one fits; coin a new one only if none does. This is what
keeps the taxonomy from exploding, and it is also why batching is better than
cheaper — one pass sees the existing vocabulary and the whole window at once, so
it converges on one `kind` where N separate calls would coin N synonyms.

**Attribution.** Messages are numbered in the prompt and every returned fact
names its source message. A fact assembled from several messages
(`хлеб 500` / `и молоко 300`) belongs to the **last** message of the group —
that is where it became complete, and `unique (message_pk, seq)` forces a single
owner.

**Marking.** Every message in the tail gets `extracted_at`, `extract_model` and
`extract_prompt_version`, whether or not it produced facts.

**Re-extraction** of an already-extracted message (an edit) diffs against its
existing facts: unchanged facts are left alone, changed ones updated, absent
ones tombstoned. A tombstone is never lifted by an extraction pass — only an
explicit un-delete by the user clears `deleted_at`.

The extraction rules corpus from the current system prompt is kept verbatim:
the loan sign convention, "a leading amount is an expense, whatever follows it",
and date-of-fact versus mentioned-date. That prose is accumulated judgement
about real messages; the schema changed, the judgement did not.

## Answering

`/q <question>` runs three steps.

1. **Question → query spec.** The model returns a constrained structure, not
   SQL: `{kind | kinds, period, group_by, aggregate, filters}`. The observed
   taxonomy is in the prompt so it knows what kinds exist.
2. **Spec → SQL → numbers.** Our code builds the query. Deterministic
   arithmetic, no LLM summation.
3. **Numbers → prose.** The model renders the result as an answer.

The spec approach is not chosen out of injection fear — it is chosen because
the aggregate is not always `sum`. Expenses sum. Weight is a series: last,
min, max, trend. Habits are counts over a period. Debts are a running sum per
counterparty — the old registry already knew this, as `balance(Заёмщик, Сумма)`
against `sum_by_period`. The enumerated set is:

`sum`, `count`, `avg`, `min`, `max`, `last`, `balance_by`.

A question the spec cannot express gets an honest "не понял, переформулируй",
not a wrong number. Text-to-SQL as an escape hatch is explicitly out of scope.

**Backlog latency.** The first `/q` after a quiet week pays for a week of
messages before it answers. Default behaviour: reply «разбираю N сообщений…»
immediately, then answer. No background flush in this version.

## Acknowledgement and errors

Dropping the echo removes the only channel through which failures reached the
user. Two replacements:

- **Extraction failures are reported, not silent.** A message whose extraction
  errored keeps `extracted_at` null and records the reason in `extract_error`;
  `/q` reports «N сообщений не удалось разобрать» alongside the answer.
- **Receipt is a reaction the bot places on the incoming message** — 👀, one
  `setMessageReaction` per stored message. It says "received and kept", and it
  is also the delete affordance: see below. One call does both jobs, and at
  roughly one request per second per chat it costs nothing for a personal bot.

## Edit and delete

**Edit** arrives as `edited_message` and re-extracts that one message. For an
unextracted message this is a text overwrite and nothing more — cheaper than
today.

**Deletion cannot be observed.** The only deletion update in the Bot API is
`deleted_business_messages`, which requires a `business_connection_id`; for a
plain bot a user deleting a message is invisible. This is settled, not open.

So deletion is explicit, and it is **any reaction the user puts on their own
message**: it tombstones that message's facts, and removing the reaction clears
`deleted_at`. The bare `-` marker goes.

**Why any reaction rather than a designated one.** The Bot API cannot restrict
which emoji a chat offers — `setMessageReaction` sets the *bot's* reaction and
`available_reactions` is read-only, and even in the client the restriction is a
group/channel admin feature that a private chat does not have. Since the picker
cannot be narrowed, the accepted set is widened instead: whatever is nearest to
hand works, and hunting for one specific emoji is never required.

**The receipt reaction is the affordance.** Because the bot has already placed
👀 on the message, the user taps that existing bubble — one tap, no picker —
and that is the delete gesture. 👎 stays a documented convention, not a
condition.

Measured 2026-09-11 against the live bot, all four halves of this:

- `message_reaction` arrives in a private chat with no administrator.
- Removing a reaction arrives as its own update with an empty `new_reaction`.
- A bot may react to the *user's* message; the bot's own reaction produces no
  update, so there is no feedback loop.
- Tapping a bubble the bot already placed produces a full update from the user
  — and `old_reaction` came back empty even though the bot's 👀 was on the
  message. **The two reaction lists are per-user, not the message's total**, so
  the handler never has to work out whose reaction it is looking at.

## Importing existing history

Without it the first `/q` runs against an empty taxonomy and the extractor
invents its vocabulary from one window. With it, it sees what kinds actually
occur in this chat and converges immediately.

**The workbook is not a source** — it never stored the original message text.
The source is the Telegram Desktop JSON export of the chat, which has the
original text, dates and per-chat message ids.

Requirements:

- **The bot's own replies are in the export and must not become messages.**
  Only the user's messages are sources.
- **Old `-` markers, `/link`, `/start` and friends import with
  `extractable = false`.** They are history; they are not facts.
- **Import does not run through `/q`.** Several thousand messages cannot hang
  off the first question — it is its own command, chewing through them in
  batches.
- **Overlap is a free correctness check.** The bot already has its own rows for
  the last few days. Importing across that period shows whether the export's
  `id` matches the `message_id` the bot saw. If it does, `upsert_message` makes
  the import idempotent and re-running it is safe.

### The workbook is not a source, but it is a labelled set

The workbook cannot supply message text, but it can supply what the *old*
extractor made of that text, joined to the export by message id at no cost.
Column A of the pre-`9540232` worksheets (`Outcome`, `Loan`, `Wish`) is the bare
`message.message_id`; after `9540232` it is `<message_id>_<seq>`. Both start
with the id, so the join is on an integer and it spans the whole life of the
bot, not only the two days the current scheme has existed.

That yields pairs of *(original text, facts the old system extracted)*,
including any row the user corrected by hand. Three uses, by value:

- **An evaluation set for the new extractor.** Run the batch pass over the same
  messages and diff its output against the old rows. This is the only way to
  find out whether the window and the converging taxonomy actually help rather
  than to assume they do.
- **Import gaps become visible.** A sheet row whose `message_id` is absent from
  the export means that message was deleted from the chat — the workbook is its
  only remaining trace. What to do with those is an open decision: the user kept
  the fact and removed the message.

**Rows that do not join cleanly are dropped from the labelled set, not
reconciled.** Besides deletions, the workbook carries duplicates from
connectivity retries and the like. In the pre-`9540232` worksheets one message
produced one row, so the same bare id appearing twice in a worksheet is a
duplicate; under `<message_id>_<seq>` several rows per message are normal and
only a repeated full key is. Either way the pair is ambiguous, and an
evaluation set is worth nothing if its labels are guesses — drop and count
them, do not repair them.
- **The id check** above, for free.

**The comparison must not reintroduce gspread.** The dependency and the
service-account plumbing go in the first commit and do not come back for this.
The workbook is exported to CSV by hand and the importer reads files. It is a
one-time operation.

## What is deleted

First commit on the branch, before any new code:

- `telegrind/sheets.py`
- `telegrind/projection.py`
- `telegrind/registry.py` in its worksheet form (the extraction-rules prose
  moves to the new prompt builder; the `_categories` parsing goes)
- `Fact.worksheet`, `Fact.sheet_key`, `Fact.origin`
- `/link`, `/unlink`, `/rebuild`, `/reload`, and `/import` in its current
  sense — the name is reused by the history importer below, which reads a
  Telegram export rather than a worksheet
- the `gspread-asyncio` dependency and the Google service-account plumbing

### Why not a new repository

The rewrite is large enough to raise the question. Three reasons it stays here.

**The production database holds real messages**, behind three migrations that
`entrypoint.sh` applies on deploy. A new repo means either abandoning that data
or writing the same migration anyway, without the chain it belongs to.

**The ingest half is already right for the new product.** `upsert_message`,
`LoggedMessage`, the raw JSONB, edit semantics, bigint ids, the uniqueness
constraint — fiddly code that works and is tested. The new product starts
exactly there: store unconditionally.

**Deletion is visible in a diff.** A new repository makes what you chose not to
carry invisible; `git rm` in the first commit makes it a record.

The one real argument for a clean slate — that the old shapes drag on the
design — is answered by that same first commit.

## Non-goals

- ASR for voice notes. Voice is stored and not extracted, exactly as today.
  Deferred extraction is the pipeline voice will need, and it slots in with no
  new machinery when ASR lands, but that is not this scope.
- Voice as a question. `/q` is text.
- Any background or scheduled extraction.
- Rollups, reminders, and the workbook in any form.
- Text-to-SQL.
- Promoting a numeric column out of JSONB.

## Probes

**Reaction mechanics — RESOLVED, see *Edit and delete*.** In summary, yes:
(2026-09-11, measured against `@assinstantbot`). The schema's "the bot must be
an administrator in the chat" is vacuous in a private chat: with
`allowed_updates` including `message_reaction`, setting a reaction produced
`old_reaction: [] → new_reaction: [🙏]` and removing it produced a separate
update with `new_reaction: []`, `chat.type: private`, no admin rights anywhere.
In aiogram, registering the handler *is* what subscribes the update type —
`allowed_updates` is derived from the observers that have handlers, so the
delete UX costs one handler and no configuration.

**Do exported message ids match what the bot saw?** Covered by the overlap check
in *Importing existing history*.

## Build order

1. Delete. One commit, no new code.
2. Migration: `message` extraction-state columns, `fact` reshaped.
3. Ingest: store unconditionally, mark `extractable`, no reply.
4. Batch extraction over a window, with the observed taxonomy in the prompt.
5. `/q`: extract the tail, then the query spec, SQL, prose answer.
6. Reaction-driven delete: 👀 placed on ingest as the receipt, and a
   `message_reaction` handler that tombstones and restores.
7. History import.
