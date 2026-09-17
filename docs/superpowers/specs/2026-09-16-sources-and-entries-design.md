# Many sources, one log — design

Status: **current**, rewritten 2026-09-17 after the user narrowed the scope:
*the schema must admit imports; the machinery around them is not built.*

Supersedes `2026-09-16-history-import-design.md` on the question *where do old
facts come from* — they come from the v1 workbook, exported to CSV. That
document's importer is parked; see *What happens to the parked modules*.

## Why this exists

The bot was built on one assumption: a fact is derived from a Telegram message,
therefore `fact.message_pk` is NOT NULL and the message is the unit of
re-derivation, the anchor of the delete gesture and the trigger for
re-extraction. That assumption is wrong in two directions.

- **The v1 workbook is already structured.** Its expense sheet is facts, not
  prose. Running thousands of model calls to re-derive what the sheet already
  states buys a worse answer than the sheet contains, and it cannot coin a kind
  — `taxonomy.observed` shows the extractor only the kinds already in `fact`, so
  a kind absent from the first pass never enters the vocabulary. Declared data
  has no bootstrap problem.
- **More sources are coming**: receipt photos, bank statements. Some arrive
  already structured (a statement line), some raw and needing the extractor (a
  receipt). Both arrive by two routes — through the chat, and from the side.

So the log needs a unit that is not a Telegram message. That unit is what this
document specifies, and it is nearly all of what this document specifies.

## Decisions this design rests on

Settled with the user, 2026-09-16 and 2026-09-17:

1. **Sources produce both shapes** — already-structured and raw. One pipeline
   must admit both.
2. **Sources arrive by both routes**, from the start: through the chat, and
   side-loaded with no message at all.
3. **No cross-source event identity.** A purchase arriving both as a
   photographed receipt and as a statement line will be two facts. Sources are
   kept from overlapping by agreement, not by the schema. A «same event» link
   can be added later as its own table; source precedence is guesswork until a
   real statement has been looked at, and is not designed now.
4. **No import machinery is built.** No import command, no import-run record, no
   summary message, no rollback gesture. The schema must *allow* imports; the
   imports themselves are one-off jobs done by hand with Claude, as the need
   arises. The user's reasoning, which this document adopts: machinery has to be
   maintained, nothing is asking for it yet, and receipts and statements will be
   imported as and when they happen.
5. **Only the v1 expense sheet is imported.** Loans (46 rows) the user enters by
   hand through the bot; the wish sheet holds stale data and is dropped.

Taken by the author, recorded so they can be overruled:

6. **`source` is a string column, not a table.** A table earns its place when
   sources acquire configuration (per-bank column mappings). Promoting a string
   to an FK later is a migration, not a redesign.
7. **`verdict` moves to the entry.** Keeping it on `message` while extraction
   state lives on the entry would be two flags that can disagree about whether
   something gets extracted — the `extractable`-vs-`verdict` defect this repo
   already shipped and documented (`db9de98`). The ingest invariant is
   unaffected: the first transaction writes two rows instead of one and still
   commits before any model call.
8. **The entry duplicates the message's text** in `content`, so the extraction
   queue is a single-table indexed read. There is exactly one writer —
   `upsert_message`, which already updates the text and clears the extraction
   state together.
9. **The entry does not duplicate `message.raw`.** Who wrote a message and what
   it replies to are Telegram facts, read through `entry.message_pk`. A
   side-loaded entry has no author and no reply edge, and the prompt does not
   state them.
10. **The table is named `entry`, not `record`.** `handlers.record` is already a
    function, and the product is a log — a log's units are entries.

## The schema

### `entry` — the unit that yields facts

| column | meaning |
|---|---|
| `id` | PK |
| `chat_pk` | FK `chat.id`, the owner |
| `source` | `telegram`, `v1-expenses`, later `kaspi`, … |
| `external_id` | text; the source's own key. Telegram: `str(message_id)`. A sheet row: its column-A key. A statement: the transaction id. |
| `message_pk` | FK `message.id`, **nullable** — set only for chat-borne entries |
| `occurred_at` | when the thing happened; `tg_date` for a message, the row's own date for an import |
| `content` | text the extractor reads; NULL for a structured entry |
| `raw` | JSONB, **nullable** — the source row verbatim, for imported entries. NULL for chat entries, whose verbatim copy is `message.raw`. |
| `verdict` | moved from `message`; one of `fact`/`question`/`talk`/`system`, never null |
| `extracted_at`, `extract_model`, `extract_prompt_version`, `extract_error` | moved from `message` |
| `created_at` | |

Constraints and indexes:

- `UNIQUE (chat_pk, source, external_id)` — this is what makes a re-import
  idempotent: the same file row finds the same entry.
- `UNIQUE (message_pk)` where `message_pk IS NOT NULL` — one entry per message.
- An index supporting the queue: `(chat_pk, verdict, extracted_at, occurred_at)`.

`entry.raw` is deliberately **not** shown to the model: `taxonomy.observed`
reads `fact.fields` only. It is where a source's columns go when they should be
preserved but must not enter the extractor's vocabulary.

`source` is also the undo. Everything one import wrote is
`WHERE source = '<that source>'` — one statement, which is what makes decision 4
safe without a run record.

### `message` — unchanged except for what leaves

Keeps: `message_id`, `kind`, `text`, `transcript`, `transcript_model`,
`audio_file_id`, `audio_duration`, `tg_date`, `edited_at`, `raw`, `created_at`,
`receipt_emoji`.

Loses, to `entry`: `verdict`, `extracted_at`, `extract_model`,
`extract_prompt_version`, `extract_error`.

It becomes what its docstring already claims — the verbatim log of what Telegram
sent, carrying no derivation state.

### `fact` — one column changes

`message_pk` becomes `entry_pk` (FK `entry.id`, `ON DELETE CASCADE`). The
partial unique index becomes `uq_fact_entry_pk_seq_live` on
`(entry_pk, seq) WHERE deleted_at IS NULL` — **created anew on the new column**,
never renamed: an index renamed onto a different column guards nothing and fails
silently.

Everything else about `fact` is untouched: `chat_pk`, `kind`, `at`, `fields`,
`deleted_at`, the GIN index, the live-facts index.

### What is not affected

`query.py`, `answer.py` and `taxonomy.py` read `fact` alone and never mention a
message. The whole answering half — question → spec → SQL → prose — needs no
change. Verified by reading, 2026-09-16.

## The paths

### From the chat

`handlers.record` writes `message` **and** its `entry` in one transaction with
`verdict=fact`, commits, then calls the classifier, then refines
`entry.verdict` in a second short transaction. The order is the existing one and
for the existing reason: aiogram advances the polling offset as it dispatches, so
an update lost mid-handler is never redelivered, and a model call before the
commit is a permanent data-loss window.

An edit updates both rows and clears the entry's extraction state, which is what
puts the message back in the queue. `record_edited` keeps its mirror order —
read, classify, write.

### The delete gesture

A reaction on a message resolves to its entry and tombstones that entry's facts;
removing the reaction restores them. One extra hop, identical behaviour. There
is no second arm — see decision 4.

### Side-loaded, raw

An entry with `content`, no `message_pk`, `verdict=fact`. It enters the same
queue as a chat message and is extracted by the same pass. There is no second
extraction path; that is the point of the design.

### Side-loaded, structured

An entry with `content = NULL` whose facts are written when it is created.
`extracted_at` is stamped then, `extract_model` stays NULL: the entry yielded its
facts and no model was involved. It is excluded from the queue twice over — by
the empty content and by the stamp — so no «do not extract me» flag exists to
disagree with anything.

## The extraction queue

`unextracted_tail` selects from `entry` alone: `extracted_at IS NULL`,
`verdict = 'fact'`, content non-empty after trimming, ordered by
`(occurred_at, id)`, limited.

`build_prompt` then loads the messages for whichever of those entries have one,
and states the author and the reply edge from there. `context_before` walks
neighbouring **entries** by `occurred_at`, filtered by the same content test.

`BATCH = 20` stays. It is a correctness bound, not a throughput knob: `_pass`
makes one model call for the whole tail against `llm.MAX_TOKENS = 2048`, and
`llm.use_tool` never inspects `response.stop_reason`, so a truncated reply still
stamps the whole tail as extracted.

## The one-off v1 expense import

Not shipped code. A throwaway script, run once against dev and once against
prod, then discarded. It is specified here because the schema above has to be
right for it, and because the mapping below is the answer to «what does an
imported entry actually look like».

Source: `/home/me/Загрузки/Aicha - Expenses.csv`, 3548 rows, 2023-06-07
onward, profiled 2026-09-16. Exported by hand from the v1 Google workbook;
the sheet is now called `Expenses` and was `Outcome` when the earlier spec was
written. `source = 'v1-expenses'`, `kind = 'expense'` for every row.

| column | destination | note |
|---|---|---|
| `#` | `entry.external_id` | 234…10457, unique, a running counter — **not** a Telegram message id |
| `Сумма` | `fields.amount` | 631 rows carry a comma decimal separator |
| `Валюта` | `fields.currency` | upper-cased; one row reads `usd` |
| `Дата` | `entry.occurred_at`, `fact.at` | `DD.MM.YYYY`, no time; midnight at the chat's offset |
| `Комментарий` | `fields.comment` | |
| `Авто категория` | `fields.category` | 2129 rows, 20 categories |
| `Необходимость` | `fields.necessity` | the same 2129 rows: must / need / nice / waste |
| `В тенге` | `fields.amount_kzt` | 870 rows are not in KZT and cannot otherwise be summed with the rest |
| `Категория` | `entry.raw` only | a copy of `Авто категория`; zero disagreements across the 1671 rows carrying both |
| `Курс`, `дней назад` | `entry.raw` only | derivable from the two amounts; «days ago» is computed from today |

The whole row is kept verbatim in `entry.raw` regardless, so nothing above is a
lossy choice.

Three rules the mapping obeys, and the reasons, because a future import must
obey them too:

- **Field names are read out of the database, not invented.** The live extractor
  writes `amount`, `currency`, `comment`, `person` — measured against the dev
  corpus 2026-09-16. An imported fact naming its amount anything else would not
  sum together with an extracted one.
- **Numbers go through `coerce.to_json_value`** so they are real JSON numbers and
  `(fields->>'amount')::numeric` cannot fail a whole query. Dates do **not** go
  through `coerce.to_instant`, which resolves a date against a message's own
  timestamp; a sheet row's date is absolute.
- **A fact carries no provenance of its own.** `model` and `prompt_version` stay
  NULL — no model and no prompt produced it — and the source is *not* copied
  down onto the fact. It is one hop away on the entry, and a second marker
  saying the same thing is the shape of bug this repo keeps shipping. (An
  earlier draft of this document stamped the source into
  `fact.prompt_version`; that column is written in three places and read in
  none, so the stamp bought nothing and could disagree.)

**The import changes the bot's behaviour, by design.** `taxonomy.render` shows
the model every field name a kind already uses, so after this import the
extractor will start filling `category` and `necessity` on new expenses. That is
wanted — 2129 hand-checked labels are the most valuable thing in the sheet — but
it arrives silently, which is why it is written down. Dropping those two columns
into `entry.raw` instead is a one-line change if the user decides otherwise.

No new **kind** is seeded: loans and wishes are not imported, so `wish` remains
uncoined and «хочу …» keeps landing in the catch-all until some first fact
creates it.

## No data migration

v2's database is empty: 0 chats, 0 messages, own volume (`telegrind-v2_pgdata`),
own compose project, measured on latitude 2026-09-16. v1's database is a
different database on the same host and is untouched by any of this.

So the schema is created in its final shape. There is no expand/contract, no
backfill and no two-stage deploy.

**One precondition, checked immediately before the deploy** — count the rows in
v2's `message` and `fact`. «There is nothing to lose» is the only claim in this
design whose failure is irreversible, and verifying it costs one command.
`chat.sheet_url` is checked at the same time: it is dead code-wise and is still
the only pointer to the retired workbook.

Mechanically this is **one new alembic revision** on top of the existing seven —
it creates `entry` and reshapes `message` and `fact` with no data preservation.
The chain is not collapsed into a fresh initial revision: a clean migration
history is cosmetic, and rewriting the chain would make every existing database
unupgradable for no gain.

## What happens to the parked modules

Both are deleted as part of this work, and both are recoverable from git.

- **`telegrind/import_history.py`** (and its tests) imports the Telegram export
  as messages. It is built, reviewed and green, and it is unwanted under
  decision 4: carrying it forward means porting it to `entry` and keeping it
  green for a job nobody has asked for. The export JSON is still on disk and the
  code is still in git; if the readable history is ever wanted, it is a one-off
  job like any other.
- **`telegrind/workbook_compare.py`** (and its tests) joins workbook rows to
  messages by message id. The exported sheet's column A is a running counter,
  not a message id — measured 2026-09-16 — so the join it was built on does not
  exist. Its premise is gone, not just its priority.

`CLAUDE.md`'s paragraph on `import_history.py` goes with them.

## Out of scope

Named so the schema can be checked against them; no tasks:

- **Any import command, import-run record, summary message or rollback
  gesture** (decision 4).
- **Receipt photos.** A vision call turning a photo into `entry.content`, or
  directly into facts. The schema admits it as a raw entry, chat-borne or
  side-loaded.
- **Bank statements.** A per-bank adapter producing structured entries. The
  `source` string is where such an adapter is keyed, and where a `source` table
  would first earn its place.
- **Cross-source event identity** and source precedence (decision 3).
- **Deleting a fact by asking in words.**
- **Importing the loan and wish sheets** (decision 5).

## Testing

Unit tests with no database, as the rest of the suite. Four things must be
pinned, and each must be watched failing against deliberately broken code — this
repo has already shipped three tests that passed against a broken
implementation:

1. `(chat_pk, source, external_id)` is unique, and writing the same source row
   twice updates rather than inserts.
2. A structured entry never appears in `unextracted_tail` — asserted against an
   entry with `content=None` **and** an `extracted_at` stamp, and against each
   alone, so the test cannot pass for only one of the two reasons.
3. A reaction tombstones the facts of the reacted message's entry, and
   removing it restores them — the hop through `entry` is new and is where this
   can silently break.
4. `verdict` is passed explicitly at every entry-writing call site. A permissive
   default on the column that selects the queue silently re-admits everything —
   this is `db9de98`, the trap this repo is most likely to repeat.

**The manual walkthrough on the dev stack is required**, not ceremony. The
repo's hand-written fake sessions yield from `begin()` unconditionally and
therefore cannot see a transaction misuse; the last plan's autobegin defect was
caught only by running it. The walk: send a message, edit it, react to delete it
and react again to restore it, ask a question and get an answer, then run the
one-off expense import and ask for a monthly total that spans both the imported
facts and a freshly typed one.
