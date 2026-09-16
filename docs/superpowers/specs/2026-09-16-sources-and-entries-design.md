# Many sources, one log — design

Status: **current**, 2026-09-16. Supersedes
`2026-09-16-history-import-design.md` on the question *where do old facts come
from* — they come from the v1 workbook, exported to CSV, imported as structured
data. That document is otherwise still live: the chat importer it specifies is
built, reviewed and merged on this branch, and this design gives it a new job
(see *The chat importer's new role*).

## Why this exists

The bot was built on one assumption: a fact is derived from a Telegram message,
therefore `fact.message_pk` is NOT NULL and the message is the unit of
re-derivation, the anchor of the delete gesture and the trigger for
re-extraction. That assumption is now wrong in two directions at once.

- **The v1 workbook is already structured.** Its `Outcome`, `Loan` and `Wish`
  sheets are facts, not prose. Running 3915 model calls to re-derive what the
  sheets already state buys a worse answer than the sheets contain, and it
  cannot coin a kind — `taxonomy.observed` shows the extractor only the kinds
  already in `fact`, so a kind absent from the first pass never enters the
  vocabulary. That is why the walkthrough's 6 «хочу …» messages all landed in
  the catch-all instead of `wish`. Declared kinds have no bootstrap problem.
- **More sources are coming**: receipt photos and bank statements. Some arrive
  already structured (a statement line), some arrive raw and need the extractor
  (a receipt). Both arrive by two routes — through the chat, and from the side.

So the log needs a unit that is not a Telegram message.

## Decisions this design rests on

Settled with the user, 2026-09-16:

1. **Sources produce both shapes.** Already-structured (a row IS a fact) and raw
   (needs extraction). One pipeline must admit both.
2. **Sources arrive by both routes**, from the start: through the chat, and
   side-loaded with no message at all.
3. **No cross-source event identity.** A purchase that arrives both as a
   photographed receipt and as a statement line will be two facts. Sources are
   kept from overlapping by agreement, not by the schema. A «these are the same
   event» link can be added later as its own table without touching anything
   here; source precedence rules are guesswork until a real statement has been
   looked at, and are not designed now.
4. **A mistake in side-loaded data is fixed by re-importing**, and a whole
   import can be undone by reacting to the summary message the import posts in
   the chat.

Taken by the author, recorded so they can be overruled:

5. **`source` is a string column, not a table.** A table earns its place when
   sources acquire configuration (per-bank column mappings). Promoting a string
   to an FK later is a migration, not a redesign.
6. **`verdict` moves to the entry.** Keeping it on `message` while extraction
   state lives on the entry would be two flags that can disagree about whether
   something gets extracted — exactly the `extractable`-vs-`verdict` defect this
   repo already shipped and documented (`db9de98`). The ingest invariant is
   unaffected: the first transaction writes two rows instead of one, and still
   commits before any model call.
7. **The entry duplicates the message's text** in its `content` column, so the
   extraction queue is a single-table indexed read. There is exactly one writer
   (`upsert_message`, which already updates the text and clears the extraction
   state together).
8. **The entry does not duplicate `message.raw`.** Who wrote a message and what
   it replies to are Telegram facts, read through `entry.message_pk`. A
   side-loaded entry has no author and no reply edge, and the prompt simply does
   not state them.
9. **The table is named `entry`, not `record`.** `handlers.record` is already a
   function, and the product is a log — a log's units are entries. It avoids a
   noun/verb collision on the most-read word in the codebase.

## The schema

### `entry` — the unit that yields facts

| column | meaning |
|---|---|
| `id` | PK |
| `chat_pk` | FK `chat.id`, the owner. Unchanged in meaning from `message.chat_pk`. |
| `source` | `telegram`, `v1-sheet`, later `kaspi`, … |
| `external_id` | text; the source's own key. Telegram: `str(message_id)`. A sheet row: its column-A key. A statement: the transaction id. |
| `message_pk` | FK `message.id`, **nullable** — set only for chat-borne entries |
| `run_pk` | FK `import_run.id`, **nullable** — set only for imported entries |
| `occurred_at` | when the thing happened; `tg_date` for a message, the row's own date for an import |
| `content` | text the extractor reads; NULL for a structured entry |
| `verdict` | moved from `message`; one of `fact`/`question`/`talk`/`system`, never null |
| `extracted_at`, `extract_model`, `extract_prompt_version`, `extract_error` | moved from `message` |
| `created_at` | |

Constraints and indexes:

- `UNIQUE (chat_pk, source, external_id)` — **this is what makes re-import
  safe**: the same file row finds the same entry.
- `UNIQUE (message_pk)` where `message_pk IS NOT NULL` — one entry per message.
- An index supporting the queue: `(chat_pk, verdict, extracted_at, occurred_at)`.

### `import_run` — one act of importing

| column | meaning |
|---|---|
| `id` | PK |
| `chat_pk` | FK `chat.id` |
| `source` | the same string the entries carry |
| `label` | what was imported, for the summary line (e.g. the file name) |
| `started_at`, `finished_at` | |
| `entries`, `facts` | counters, as written |
| `anchor_message_pk` | FK `message.id`, nullable — the summary message posted in the chat; reacting to it reverts the run |
| `reverted_at` | nullable |

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
removing the reaction restores them. One extra hop, identical behaviour.

### Side-loaded, raw

An entry with `content`, no `message_pk`, `verdict=fact`. It enters the same
queue as a chat message and is extracted by the same pass. There is no second
extraction path — that is the point of the design.

### Side-loaded, structured

An entry with `content = NULL` whose facts are written at import time.
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

## The CSV import

### Entry point

A CLI subcommand. The bot is not involved and need not be stopped: no model call
is made, so the truncated-pass hazard that governs the chat importer's runbook
does not exist here.

```
uv run python -m telegrind.import_sheet Outcome.csv Loan.csv Wish.csv \
    --chat-id 3260987 --source v1-sheet [--dry-run]
```

### Mapping

- **Column A** is the row's `external_id`. v1 wrote either `<message_id>` or
  `<message_id>_<seq>` (after message 9540232). The seq half is **not
  discarded**: it becomes `fact.seq`, so two facts on one v1 message stay two
  facts. Dropping it collides them on `uq_fact_entry_pk_seq_live`.
- **The sheet name** is the fact's `kind`, through a mapping declared explicitly
  in the module — `Outcome` → `expense`, `Loan` → `loan`, `Wish` → `wish`. Not
  inferred from the file name.
- **The date column** gives both `entry.occurred_at` and `fact.at`. It is an
  absolute date, so it does **not** go through `coerce.to_instant`, which
  resolves a date against a message's own timestamp.
- **Every other column** becomes a key in `fact.fields`, with values passed
  through `coerce.to_json_value` so that numbers are real JSON numbers and
  `(fields->>'amount')::numeric` cannot fail a whole query.
- `fact.prompt_version` is set to the source string (`v1-sheet`), and
  `fact.model` stays NULL. A fact nobody's model produced must not be findable
  by a re-extraction pass looking for facts from an older prompt.

**The exact column mapping is not in this document**: it is derived from the
headers of the user's export and confirmed with him before implementation. The
rules above are what the mapping must satisfy.

### All or nothing

One transaction for the whole run. The run **refuses to start** if any row fails
to map — an unparsable key, an unparsable date, an unknown sheet. An import that
«mostly worked» cannot later be told from a correct one, and the rows it dropped
are unfindable. `--dry-run` reads, maps, reports and writes nothing; the real
run repeats the mapping and commits.

### Idempotency

A re-run finds the same entries by `(chat_pk, source, external_id)` and replaces
their facts through `store.replace_facts`, which diffs by `seq`: changed facts
are updated, vanished ones tombstoned, new ones added. Re-importing a corrected
file is the supported way to fix imported data.

### The summary, and the rollback

At the end of a run the command posts one message into the chat — source, rows,
facts, totals by kind — stored like everything the bot says, with
`verdict=system` on its entry so it never enters the queue. Its `message.id` is
the run's `anchor_message_pk`.

Reacting to that message tombstones every live fact of every entry in the run and
stamps `reverted_at`; removing the reaction restores them. This is one explicit
branch in `handlers/reactions.py`: the reacted message is looked up as a run
anchor first, and only if it is not one does the handler fall back to tombstoning
that single message's facts. Both arms share `tombstone_facts` /
`restore_facts`; the anchor arm passes the run's entries instead of one.

`--dry-run` posts nothing. The command needs `BOT_TOKEN` to post the summary at
all, and a run started without one fails before it writes, rather than importing and
leaving a run whose only supported undo gesture was never posted.

After a revert the entries remain. Re-importing writes fresh facts; the
tombstoned ones do not collide, because the uniqueness on `(entry_pk, seq)` is
partial on `deleted_at IS NULL` — which is why it was made partial.

## No data migration

v2's database is empty: 0 chats, 0 messages, own volume (`telegrind-v2_pgdata`),
own compose project, measured on latitude 2026-09-16. v1's database is a
different database on the same host and is not touched by any of this.

So the schema is created in its final shape. There is no expand/contract, no
backfill and no two-stage deploy.

**One precondition, checked immediately before the deploy** — count the rows in
v2's `message` and `fact`. «There is nothing to lose» is the only claim in this
design whose failure is irreversible, and verifying it costs one command.
`chat.sheet_url` is checked at the same time: it is dead code-wise and is still
the only pointer to the retired workbook.

Mechanically this is **one new alembic revision** on top of the existing seven —
it creates `entry` and `import_run` and reshapes `message` and `fact` with no
data preservation. The chain is not collapsed into a fresh initial revision:
a clean migration history is cosmetic, and rewriting the chain would make every
existing database unupgradable for no gain.

## The chat importer's new role

`telegrind/import_history.py` stays. Its job changes from «the source of old
facts» to **restoring the readable history**: it imports the export's messages
and their entries, and those entries do **not** yield facts.

The reason is decision 3. A purchase that exists as a `v1-sheet` row and as its
own chat message would otherwise be counted twice, because they are now two
entries from two sources. Making the history import fact-free keeps the money
correct while making the log readable.

That change is **not in this plan's scope**. It is recorded here so that whoever
runs the chat importer next knows it must not extract, and `import_history.py`
carries a pointer to this paragraph.

`telegrind/workbook_compare.py` also changes role: it was built to decide
whether the old facts should be imported at all, and the user has answered that.
It becomes the verification tool — after the CSV import, it checks each old
workbook row against the fact that now exists for it. Its docstring's claim that
«the workbook cannot be an import source» is true of *messages* and false of
*facts*, and is corrected when the module is next touched.

## Out of scope

Named so the schema can be checked against them, with no tasks in this plan:

- **Receipt photos.** A vision call that turns a photo into `entry.content`, or
  directly into facts. The schema admits it as a raw side-loaded (or chat-borne)
  entry.
- **Bank statements.** A per-bank adapter producing structured entries. The
  schema admits it; the `source` string is where the adapter is keyed, and is
  where a `source` table would first earn its place.
- **Cross-source event identity** and source precedence (decision 3).
- **Deleting a fact by asking in words.** A genuinely useful feature, and not
  part of the import story.
- **Re-running extraction over the imported history** (see above).

## Testing

Unit tests with no database, as the rest of the suite. Five things must be
pinned, and each must be watched failing against deliberately broken code — this
repo has already shipped three tests that passed against a broken
implementation:

1. `(chat_pk, source, external_id)` is unique, and a second import of the same
   row updates rather than inserts.
2. A structured entry never appears in `unextracted_tail` — assert it against an
   entry with `content=None` **and** an `extracted_at` stamp, and against each
   alone, so the test does not pass for only one of the two reasons.
3. Reverting a run tombstones exactly its own run's facts — the assertion needs
   a second run's facts present and untouched, or it cannot discriminate.
4. A row that fails to map aborts the whole run and writes nothing.
5. `verdict` is passed explicitly at every entry-writing call site. A permissive
   default on the column that selects the queue silently re-admits everything —
   this is `db9de98` and it is the trap this repo is most likely to repeat.

**The manual walkthrough on the dev stack is required**, not ceremony. The
repo's hand-written fake sessions yield from `begin()` unconditionally and
therefore cannot see a transaction misuse; the last plan's autobegin defect was
caught only by running it. The walk: send a message, edit it, react to delete it,
import the CSVs, ask for a monthly total and confirm it spans both sources,
react on the summary to revert, confirm the total drops, remove the reaction,
confirm it returns, re-import and confirm nothing doubles.

One thing to look at during the walk, with no test attached: after the import,
`taxonomy.observed` contains `wish` and `loan`. The import seeds the vocabulary,
which is what fixes the bootstrap problem named at the top of this document.
