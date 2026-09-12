# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Telegrind is an async Telegram bot that keeps a personal log. You write what
happened in natural language; every message is stored verbatim in Postgres on
arrival, and a batch pass derives facts from it when you ask. The dialogue is
the product — there is no spreadsheet. The bot uses aiogram, SQLAlchemy
(asyncpg) and the Anthropic API.

The design this is being built to is `docs/superpowers/specs/2026-09-11-dialogue-first-design.md`;
Phase 1 (store, react, tombstone) and Phase 2 (batch extraction and `/q`) are
done, Phase 3 (history import) is not.

## Running the Project

**With Docker (preferred):**
```bash
docker compose up
```

**Locally:**
```bash
uv sync
python main.py
```

Copy `.env.dist` to `.env` and fill in `BOT_TOKEN`, `DATABASE_URL` and
`ANTHROPIC_API_KEY` before running, or run `./dev-setup.sh` which writes both
`.env` and a `compose.override.yml` for you.

`.env` carries the **host** database URL (`localhost:5433`) so `alembic` and
`pytest` work from a shell. `compose.yml` overrides it for the bot service,
which reaches postgres by service name.

```bash
uv run pytest          # no live database, no network
uv run ruff check      # and `ruff format`
uv run ty check
```

## Deployment

Production runs on **`latitude`** (Debian 13, tailnet `100.64.0.8`) as the docker
compose project `telegrind`, brought up by hand on 2026-08-01.

**Nothing auto-deploys. A push to `main` deploys nothing.** The poll-and-build
pipeline described in the `vps` repo was a PowerShell Scheduled Task built for the
old `server` box; that box left the fleet on 2026-08-01 and latitude has no `pwsh`
and no port of the engine. Every deploy today is manual, on latitude:
`git pull` in `homeserver/telegrind/src/`, then
`docker compose -f homeserver/telegrind/compose.prod.yml up -d --build`.

- `g513ie` is the **hostname of this laptop** (fleet name `g15`), not the server.
  An earlier revision of this file named it as the homeserver; that was wrong.
- `homeserver/telegrind/src/` is a gitignored clone the pipeline fast-forwards when
  it exists. Never edit it in place — commit in a real checkout and pull.
- The prod compose lives in `vps` (`vps/homeserver/telegrind/compose.prod.yml`), not
  here — this repo carries only the dev `compose.yml`. Prod secrets (`.env.prod`)
  live in `vps/homeserver/telegrind/`, outside the `./src` build context.
- Prod postgres publishes **no port**: it is reachable only from inside the compose
  network, or through an SSH tunnel to latitude.
- The prod stack is project `telegrind` with the named volume `telegrind_pgdata`; the
  dev stack is project `telegrind-dev`. Never `docker compose down -v` on prod — it
  orphans the database volume, and the bot crash-loops on a fresh `pgdata` because
  alembic has no version seed.

Full runbook: `vps/homeserver/DEPLOYING-A-REPO.md`.

## Telegram Bot API

`docs/telegram-bot-api.md` is the curated Bot API surface for this bot: what
telegrind already uses, what is callable at the pinned aiogram version, what
needs a bump, and what is ruled out and why. **Read it before proposing or
building anything that touches the Telegram side** — it is verified against
the installed `aiogram` tree, not against the changelog, so it says what can
actually be called here.

## Architecture

### Request flow

```
Telegram message
  → Dispatcher (aiogram)
  → populate_chat_data middleware   # injects: session, chat, config
  → handler (handlers/handlers.py)  # store.upsert_message, then one reaction
```

Asking is the second trigger, and the only one that parses anything:

```
/q <question>
  → handlers/query.py               # store the question, extractable=False
  → extract.run                     # the whole unparsed tail, in one call
  → answer.spec_for                 # question → a closed query spec
  → query.run                       # the spec → SQL → numbers
  → answer.render                   # numbers → one or two sentences
```

There is no reply on ingest. The only outward signal there is
`setMessageReaction` with 💔, and tapping that same bubble is how the user
deletes:

```
Telegram message_reaction
  → populate_chat_data middleware
  → handlers/reactions.py           # tombstone the message's facts, or restore
```

### Key layers

**`telegrind/bot/middleware.py`** — `populate_chat_data` runs before every
update. It resolves (or creates) the `Chat` row, and injects it, the SQLAlchemy
session and a `ChatConfig` into the handler kwargs. It narrows on the update
type: `Message` and `MessageReactionUpdated` pass, everything else is dropped.

**`telegrind/bot/handlers/handlers.py`** — ingestion. The slash catch-all first
(stored with `extractable=False`, so a command never coins a category), then
voice, then a filterless catch-all so a sticker or a photo is stored too.
`acknowledge` places the receipt and never raises: the row is already
committed, so a Telegram failure costs a visual cue and nothing else. An edit
advances the receipt along `RECEIPT_CYCLE` (💔 → ❤‍🔥 → 💘), which is the only
signal that the bot noticed the edit — every emoji in it stays a heart, because
tapping any of them still deletes.

**`telegrind/bot/handlers/reactions.py`** — *any* user reaction tombstones the
message's facts; removing it restores them. Registering the observer is what
subscribes the `message_reaction` update type.

**`telegrind/store.py`** — the message and fact repository. `upsert_message`
appends on first sight and overwrites on edit, clearing `extracted_at` so the
next batch pass picks the message up again. A forwarded message is dated by its
origin, not by the forward. `unextracted_tail` and `context_before` are the
window a pass reads; `replace_facts` diffs a message's facts by `seq`, so a
re-extraction updates what changed and tombstones what disappeared.

**`telegrind/taxonomy.py`** — the chat's own `kind`/field vocabulary, read
back out of `fact`. There is no registry of permitted kinds; showing the
extractor what already exists is the only thing standing between a free-form
`kind` and a hundred synonyms for "expense".

**`telegrind/extract.py`** — the batch pass. `build_prompt` states each
message's local clock, who wrote it, and which message it replies to;
`drafts_from` coerces what comes back, and anything it cannot place becomes a
complaint rather than a silent drop. `run` does the tail, `run_for` does one
edited message — through the same window builder, because a message
re-extracted alone coins a different `kind` than it would in company.

**`telegrind/query.py`** — a closed set of aggregates (`sum`, `count`, `avg`,
`min`, `max`, `last`, `balance_by`) and the SQL for them. A question that does
not fit is refused, never approximated. `jsonb_typeof(fields->'x') = 'number'`
guards every cast, which is exact rather than heuristic precisely because
`coerce.py` already made anything parseable a real JSON number.

**`telegrind/answer.py`** — the two model calls that bracket the arithmetic:
question → spec, then numbers → prose. Between them sits Postgres, and the
model is never asked to add anything up.

**`telegrind/bot/handlers/receipts.py`** — the receipt emoji, its cycle and
`acknowledge`. Split out of `handlers.py` because it registers nothing:
`query.py` needs it, and importing it from `handlers.py` would run that module
— registering its slash catch-all — before `/q`'s own handler.

**`telegrind/coerce.py`** — the write boundary for a fact field. A value that
parses becomes a real JSON number, so `(fields->>'amount')::numeric` cannot
fail the whole query; one that does not stays text and simply never aggregates.
`to_instant` resolves a date against the **message's own** timestamp, in the
chat's timezone.

**`telegrind/config.py`** — `ChatConfig`, the timezone offset and default
currency, read off the `chat` row.

**`telegrind/models.py`** — `Chat`, `File`, `LoggedMessage` (table `message`)
and `Fact`. A fact is service columns plus a JSONB `fields`: only `kind` and
`at` are promoted out, because every query filters on both. `deleted_at` is a
tombstone, and the uniqueness on `(message_pk, seq)` is a *partial* index so a
tombstoned fact does not collide with the row that replaces it.

**`telegrind/llm.py`** — the client, the model names, the two call helpers
(`use_tool` forces a tool call; `say` returns prose) and the prompts.
`EXTRACTION_RULES` is accumulated judgement about real messages, composed into
`EXTRACT_SYSTEM` with the observed taxonomy. `PROMPT_VERSION` is extraction's
and is what `fact.prompt_version` records; the query and prose prompts persist
nothing and are not versioned.


## Working in this repo — traps that have already cost time

**The suite is not the gate the prompt is.** `addopts = "-m 'not llm'"`, so a
plain `uv run pytest` deselects `tests/test_extraction_quality.py` entirely.
Run it with `uv run pytest -m llm`; it hits the Anthropic API and costs tokens,
and it needs `ANTHROPIC_API_KEY` in the environment or every case **silently
SKIPs rather than fails**. It is also a stochastic oracle — real API, default
temperature — so a case measured 10/10 in isolation can still fail roughly 2
runs in 18. Probe a suspect case several times before believing one failure.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**A gate that runs under conditions production never sees is worth nothing.**
The `-m llm` gate went 24/24 green while the live chat kept filing measurements
in the catch-all kind, because the gate rendered the chat vocabulary as
`"(пусто)"` — the one condition under which a catch-all cannot swallow
anything. When fixing a prompt defect, write the assertion that can see it
first and watch it fail; then check that the fixture's conditions match a real
pass. And when a prompt change flips a fixture, decide which of the two is
wrong before reverting the prompt — twice the fixture was the wrong oracle
(`12 октября куплю подарок за 20000` is a `wish`, not an `expense`, because an
expense dated in the future would be counted before the money moved).
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**The manual walkthrough on the dev stack is not ceremony.** Every end-to-end
walk has found a defect the unit suite structurally could not see: an edit
timestamp arriving as a raw Unix int and killing every edit; a database URL
right on the host and wrong inside the container; a measurement landing in the
catch-all kind where no aggregate could reach its number; and two independent
facts collapsing onto one message, double-counting a sum after an edit. Do not
skip it because the suite is green.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**Never `source` this repo's `.env` in a shell.** It has CRLF line endings, so
`source` / `set -a` carries a trailing `\r` into every value — and when that
reaches an HTTP header the Anthropic client reports the illegal header *by
printing the whole API key* into the failure output. `tests/conftest.py` loads
it in-process with python-dotenv for exactly this reason. Pass `load_dotenv` an
explicit path (with no argument it resolves relative to the calling script, and
it asserts on `frame.f_back` under a `python -` heredoc), and pipe anything that
could surface a key through `sed -E 's/sk-ant-[A-Za-z0-9_-]+/sk-ant-***/g'`.
Likewise never dump `docker compose config --format json` — it prints every
resolved secret.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**A configured tool is not a running tool.** `ty` sat in `pyproject.toml` for
weeks without ever being installed, so a plan that said "expect PASS" was
written against a baseline nobody had measured — installing it surfaced 13
diagnostics. Confirm the binary exists and run it once before trusting a gate.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**Lint specifics that look like bugs and are not.** An unused `# noqa` is itself
an error here (`RUF100`), and `BLE` is not among the enabled rule sets — so
`# noqa: BLE001` on a broad `except Exception` is flagged as an unused
suppression. Write the reason as a plain comment instead. `ANN` wants a return
annotation on every function including every test and nested fake; `T20` bans
`print`; `RUF001-003` are ignored because Cyrillic literals are the app's
language. On Python 3.14, PEP 758 makes unparenthesized
`except APIError, NoValidUrlKeyFound:` valid and deferred annotations make
quoted forward references wrong (`UP037`) — do not "fix" either.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**aiogram stops handler propagation on any non-`SkipHandler` return.** A handler
that matches an update and then decides it does not want it must
`raise SkipHandler`; returning `None` silently consumes the update and no later
handler ever sees it. The symptom is a message that produces no row, no
reaction and no reply at all.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**In SQLAlchemy 2 a bare read autobegins, so `session.begin()` is not
re-entrant.** A read taken "outside a transaction" and then followed by
`async with session.begin():` raises *A transaction is already begun on this
Session*. It has bitten four separate sites. The suite cannot catch it: this
repo's hand-written fake sessions yield from `begin()` unconditionally, so only
reading the code or running against a real `Session` finds it.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**Bump `PROMPT_VERSION` only when the prompt changed meaning.** It is stamped on
every extracted fact and is what a later re-extraction pass uses to find facts
produced by an older prompt; a gratuitous bump invalidates the eval baseline and
stamps mismatched versions on facts written during live testing.
<!-- src: telegrind 35574a2 | 2026-09-12 -->
