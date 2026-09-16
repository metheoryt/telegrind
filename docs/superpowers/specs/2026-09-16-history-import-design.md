# Importing the v1 history into v2 — design

Status: **current**, 2026-09-16. This is the delta against
`2026-09-11-dialogue-first-design.md` §*Importing existing history*, which was
written before the classifier and the `verdict` column shipped and is wrong
about `extractable` in every paragraph that names it. Where the two disagree,
this document wins; where this document is silent, that one still holds — its
requirements (bot replies are not sources, import does not run through `/q`,
overlap is a free correctness check) are all carried forward below.

## What there is to migrate

Measured on latitude, 2026-09-16, with v2 already up beside v1:

- **v1's database holds nothing of the log.** `alembic_version` at
  `2700e0b3a8b6`, `chat` (62 rows, 12 of them with a `sheet_url`) and `file`
  (1 row). No `message`, no `fact` — v1 never stored the text. It projected
  into a Google workbook and kept only the pointer.
- **v2's database is empty.** 0 chats, 0 messages, own volume
  (`telegrind-v2_pgdata`), own compose project (`telegrind-v2`), own token.
- So a database-to-database migration would move a row per chat and one file
  id, and none of the history. **The history is not in Postgres and never was.**

Two sources have it, and they carry different halves:

| Source | Carries | Missing |
|---|---|---|
| Telegram Desktop JSON export of the chat | the original text, dates, per-chat message ids, reply edges | file ids, voice transcripts, a forward's origin date |
| The old Google workbook (`Outcome`, `Loan`, `Wish`) | what the *old* extractor made of each message, including rows the user corrected by hand | the text itself — it was never stored |

The export is the source of the log. The workbook is a **labelled set**, joined
to the export by message id, and it is a measuring stick, not an input.

## The export, measured (2026-09-16)

`ChatExport_2026-09-16/result.json`, 2.9 MB, plus `photos/` and
`voice_messages/` directories.

- **6837 entries**, 2023-06-05 → 2026-09-16. 4069 his, 2766 the bot's, 2 service.
- **3915 of his are candidates for `fact`** — everything that is not a command,
  not a bare `-` and not empty. Median text 17 characters, longest 100.
- **27 `/start` and nothing else.** No `/q`, no `/link` in the whole history:
  the verdict table's command arm exists for 27 rows.
- **56 bare `-` markers**, **71 entries with no readable text** (68 photos, a
  video, a voice message).
- **171 forwards, 1414 edits, 350 messages whose `text` is a list** of
  fragments — the flattening case is 9% of the corpus, not an edge case.
- **Exactly one voice message**, and the export ships the audio in
  `voice_messages/`. The lost `file_id` costs one row.
- **57 of his messages are replies, none of them to the bot.** This is the
  finding that retires a worry: v2 classifies a reply into a conversation as
  `talk` (`6c679a1`), and if the history were full of replies to the bot's
  answers, asserting `fact` for them would have been wrong several thousand
  times. It is wrong zero times.
- The first day or two (2023-06-05/06) is test junk — `asd`, `dssad`, `111`.
  The importer takes `--since` for this; the default imports everything.

**The overlap check the 2026-09-11 spec relies on does not exist here.** It
assumed the bot already had its own rows for the last few days, so the export's
`id` could be compared against a `message_id` the bot saw live. v2 is a
different bot with a different chat and an empty database, so there is nothing
to overlap with. The workbook join is now the *only* check that the export's
ids are the ids v1 recorded — which raises its value from a bonus to the one
piece of evidence.

### Batch size is bounded by `MAX_TOKENS`, not by taste

`extract._pass` makes **one** model call for the whole tail and writes the facts
for every message in it. `llm.MAX_TOKENS` is 2048. A pass of 200 messages would
need several times that to emit their facts, and the reply would be truncated —
which fails the *whole* batch, not its overflow.

So the import drives `extract.run(limit=…)` at roughly **20 messages per pass**,
not the default 200: ~2000 output tokens is about 40 facts, and 20 messages can
plausibly yield that many. That is ~200 calls over the corpus on
`claude-haiku-4-5`, which is what step 4 is for — measure it on dev rather than
trust this arithmetic.

## Scope

**Only the owner's own chat.** v2 is a second bot with a second token; the other
61 v1 chats cannot follow it, and the 11 other workbook links are their owners'
business. v1 keeps running and `telegrind_pgdata` is not touched by any of this.

**The old facts do not enter the database in this round.** The importer produces
the texts and a comparison report; whether the workbook's rows are worth
carrying into `fact` is decided from that report, not in advance. Two things
make carrying them costly enough to want the numbers first: they carry the old
taxonomy, and they have no `seq` of their own, so the first re-extraction of
their message overwrites them — `replace_facts` diffs by `seq`.

## The importer

A one-shot CLI module, `telegrind/import_history.py`, run as
`python -m telegrind.import_history <result.json> --chat-id <id>`. Not a bot
command. The 2026-09-11 spec called for a command because everything the bot
could do was a command; since then the control channel moved out to cladaeb and
a batch job that runs once has no reason to arrive over Telegram.

On latitude it runs inside the v2 stack, so it reaches a postgres that publishes
no port:

```console
docker compose -p telegrind-v2 -f compose.prod.yml run --rm \
  -v /path/to/result.json:/import/result.json:ro \
  bot python -m telegrind.import_history /import/result.json --chat-id <id>
```

### Rows are built as aiogram messages, then stored by `store.upsert_message`

The importer constructs a real `aiogram.types.Message` per export entry and
hands it to the existing `store.upsert_message`. It does **not** write
`LoggedMessage` rows directly.

The reason is `raw`. `message_values` dumps the aiogram object into the JSONB
column, and three readers parse that column afterwards: `store.by_the_bot` reads
`raw["from_user"]["is_bot"]`, `store.reply_to` reads
`raw["reply_to_message"]["message_id"]`, and `extract.author_of` reads
`is_bot` again to decide how the prompt attributes the line. A row whose `raw`
has a different shape passes every insert and then misattributes every imported
line in the extraction window. Building the aiogram object is how the shape
stays identical to live ingestion by construction rather than by agreement.

Idempotency comes free from the same call: `upsert_message` overwrites on
`(chat_pk, message_id)` and clears `extracted_at`, so re-running the import is
safe and re-importing an edited range re-queues it for extraction.

### The field mapping, and what is lost

| Export | aiogram `Message` | Note |
|---|---|---|
| `id` | `message_id` | per-chat, and the same number the bot saw live |
| `date_unixtime` | `date` | UTC |
| `edited_unixtime` | `edit_date` | **pass the raw int, not a `datetime`.** `store.edited_at` tolerates both, but `message_values` dumps the object into `raw` with `mode="json"`, where a `datetime` serializes as an ISO string and live ingestion leaves an int. Nothing reads `raw["edit_date"]` today; the point is that the whole argument for going through aiogram is that `raw` is identical to live *by construction*, and a divergence here would make that claim false |
| `from_id` (`user<N>`) | `from_user.id`, `from_user.is_bot` | `is_bot` is true for the bot's own id — see below |
| `text` / `text_entities` | `text` | the export gives a string OR a list of fragments; flatten the list by concatenating each fragment's `text` |
| `reply_to_message_id` | `reply_to_message.message_id` | a stub message is enough — only the id is ever read back |
| `media_type: voice_message`, `duration_seconds` | `voice.duration` | `voice.file_id` is **not in the export**. `aiogram.types.Voice` requires one, so write `import:<export file path>` — prefixed so that a value which cannot be fetched cannot be mistaken for one that can. Nothing reads `audio_file_id` today (checked 2026-09-16: it is written by `message_values` and read nowhere), so the placeholder costs nothing until transcription is wired, at which point the prefix is the signal to skip the row |
| `"type": "service"` | — | skipped entirely; not a message |

Known losses, all acceptable and none silent:

- **Voice messages arrive with no transcript**, and the export has no file id to
  fetch the audio with. `store._has_content()` therefore keeps them out of
  `unextracted_tail` — they are stored and wait, which is the behaviour the
  column was designed for.
- **A forwarded message loses its origin date.** The export records
  `forwarded_from` as a name and no timestamp, so `forward_origin` cannot be
  reconstructed and `tg_date` becomes the forward's own date. Live ingestion
  dates a forward by its origin; imported forwards will not match that. Count
  them and report the count.

### `verdict` is asserted from the export, never classified

Every row is written with an explicit `verdict`:

| Export entry | `verdict` |
|---|---|
| sent by the bot | `system` |
| the user's `/q …` | `question` |
| any other `/…` command | `system` |
| the old bare `-` markers | `system` |
| everything else from the user | `fact` |

No `classify.verdict_for` call is made. Two reasons, and the second is the
stronger: several thousand messages is several thousand model calls, and the
export's own structure answers "is this a command, is this the bot" *exactly*,
where the classifier only guesses. The classifier exists because a live message
arrives without that structure.

**Pass `verdict` at every call site.** `upsert_message`'s parameter defaults to
`VERDICT_FACT`, and leaning on that default is the trap project memory records
from `db9de98`: when the tail selector moved from `extractable` to `verdict`,
the permissive default sent `/start` and every command typo into the extraction
tail, and nothing failed — the rows simply got parsed. Here the same omission
would feed the extractor thousands of bot replies.

### Nothing goes through the dispatcher

The importer calls `store.upsert_message` and commits. It does not call
`routing.route`, `outbound.say`, or any handler. Routing would place a 💔
reaction on every historical message and send a reply into the chat for every
question in the log.

### Identifying the chat

The importer takes `--chat-id` and treats it as authority, because the export
cannot be trusted to state it. In a private-chat export the top-level `id` is
the *peer's* user id — the bot's — while telegrind's `chat.chat_id` for a
private chat is the user's own id. Deriving one from the other silently is how
the whole import lands under the wrong chat.

The importer therefore:

1. reads `--chat-id`, and creates the `Chat` row if absent (defaults: `tz_offset`
   6, `currency` KZT);
2. derives the bot's id as the export's top-level `id`, marks those entries
   `is_bot: true`, and **refuses to run** if the export contains a third
   distinct `from_id` — a group chat export is not what this reads;
3. prints the id it resolved and a two-line sample before writing anything, run
   under `--dry-run` first.

Cross-check available at no cost: the resolved `chat_id` must be one of the 62
in v1's `chat` table, and — since the owner's workbook is the one being exported
to CSV — one of the 12 with a `sheet_url`.

## Extraction is a separate step

Import writes messages. Nothing extracts during it.

Afterwards, `extract.run` is driven in batches of 200 over `unextracted_tail`,
one transaction per batch, until the tail is empty. The batch loop lives in the
same module behind a second entry point (`--extract`), so importing and
extracting can be run and re-run independently.

**On the dev database first.** The pass costs one model call per window over the
whole history, and the number is unknown until the export is counted. The dev
run yields the cost, the wall time, and — the thing worth more — the taxonomy
that actually converges over the real corpus. Production runs only after those
three numbers have been looked at.

`PROMPT_VERSION` is not bumped for any of this. The prompt does not change
meaning; a bump would only stamp a mismatched version on the imported facts and
invalidate the eval baseline.

## The workbook comparison

The workbook goes to CSV by hand, one file per worksheet. **gspread does not
come back** — it and the service-account plumbing were deleted in `248fe9d` and
a one-time report is not a reason to restore them.

The join is on message id: column A is the bare `message.message_id` in the
pre-`9540232` worksheets and `<message_id>_<seq>` after it, so both start with
an integer and the join spans the whole life of v1.

Rules, from the 2026-09-11 spec and unchanged:

- **Ambiguous pairs are dropped and counted, never repaired.** A repeated bare
  id in a pre-`9540232` worksheet is a duplicate from a connectivity retry; a
  repeated full `<id>_<seq>` key after it is the same. A labelled set whose
  labels are guesses is worth nothing.
- **A workbook row whose id is absent from the export is a message the user
  deleted from the chat.** The workbook is its only remaining trace. Report
  those separately; what to do with them stays open.
- **The id check is free.** If the export's `id` matches the `message_id` v1
  recorded, the whole scheme holds.

Output is a report file — counts, and per-message diffs of old kind/fields
against new. No rows are written to `fact` by the comparison.

## What this does not do

- Does not touch v1: its container, its volume and its 62 chats stay as they are.
- Does not migrate the other 61 chats or their 11 workbook links.
- Does not restore gspread, a service account, or `/link`.
- Does not write the workbook's facts into `fact`.
- Does not bump `PROMPT_VERSION`.

## Order of work

1. The export exists and is counted (messages, bot vs user, voice, forwards).
2. Importer + tests, run `--dry-run` against the dev database.
3. Real import into the dev database; walk the result by hand in the dev bot.
4. Batch extraction on dev; record cost, wall time, and the observed taxonomy.
5. Workbook CSVs, comparison report, read the numbers.
6. Decide on the old facts.
7. Import + extraction on production, inside the `telegrind-v2` stack.

Steps 4 and 6 are gates, not milestones: each ends with numbers on the screen
and a decision, not with a green suite.

## Testing

The importer is a pure transformation in front of one existing repository call,
which is what makes it testable without a database:

- **Export entry → aiogram `Message`**, per row of the mapping table above,
  including the list-shaped `text`, a reply, a voice entry and a service entry.
- **`verdict` assignment**, per row of the verdict table. The `/q` case and the
  bot-reply case are the two that cost real money when wrong.
- **`raw` shape**: assert `store.by_the_bot` and `store.reply_to` read back what
  the importer meant, through the same functions production uses. This is the
  test that would have caught writing rows directly.
- **A third `from_id` refuses to run.**
- **An entry with nothing readable** — a sticker, a photo with no caption —
  stores as `text=None` and stays out of both `unextracted_tail` and
  `context_before`, because `_has_content()` gates each. That is the live
  behaviour too, and it is the cheapest test here.
- **Idempotency**: importing the same entry twice leaves one row, and an entry
  whose text changed clears `extracted_at`.

Per the repo's own rule: before believing any of these, break the thing it
guards and watch it fail for the reason expected.
