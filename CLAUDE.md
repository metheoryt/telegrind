# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Telegrind is an async Telegram bot that keeps a personal log. You write what
happened in natural language; every message is stored verbatim in Postgres on
arrival, and a batch pass derives facts from it when you ask. The dialogue is
the product — there is no spreadsheet. The bot uses aiogram, SQLAlchemy
(asyncpg) and the Anthropic API — and, on a dev box only, a `claude -p`
subprocess, which is the meta layer and the fourth runtime dependency.

Two specs, and both are live:

- `docs/superpowers/specs/2026-09-11-dialogue-first-design.md` — the recording
  half. Phase 1 (store, react, tombstone) and Phase 2 (batch extraction and
  `/q`) are done, Phase 3 (history import) is not.
- `docs/superpowers/specs/2026-09-11-claude-meta-layer-design.md` — the Claude
  meta layer, and what `.claude/memory/project.md` means by «the spec» in its
  meta-layer bullets. Its build order runs to eight steps; 1–4 are done (the
  classifier and its verdict column, the bot storing its own messages,
  natural-language questions, and the Claude handler with its allowlist,
  queue and 👀). Step 5 is half done: forking per turn came forward into step
  4, because a derived session id does not survive three turns without it, but
  the warm base it was specified beside did not — which is why every
  conversation still pays for a cold session.

## Running the Project

**With Docker** — the recording half, and the easiest way to get one:
```bash
docker compose up
```

**Locally** — and this is the only way to run with Claude:
```bash
docker compose up postgres
uv sync -p /usr/bin/python3.14
uv run python main.py
```

**Never run a bare `uv sync`.** The repo pins no `.python-version`, so uv picks
the freethreaded CPython 3.14 it manages for itself — and `psycopg-binary`
publishes no `cp314t` wheel, so the sync fails and takes the venv with it.
Always name a non-freethreaded interpreter (`/usr/bin/python3.14` on this box).

Running on the host is not only convenience: **the Claude meta layer works
there and nowhere else.** `telegrind-bot:dev` has no `claude` binary, no
`~/.claude` and no subscription login, so inside the container the layer is
simply off — `talk` is stored, left bare, and nothing answers it.

Copy `.env.dist` to `.env` and fill in `BOT_TOKEN`, `DATABASE_URL` and
`ANTHROPIC_API_KEY` before running, or run `./dev-setup.sh` which writes both
`.env` and a `compose.override.yml` for you. `dev-setup.sh` writes only what is
needed to boot, so the `CLAUDE_*` keys are not in what it generates — copy them
out of `.env.dist` by hand, and note that an unset `CLAUDE_ADMIN_CHAT_IDS`
leaves the meta layer off without complaining.

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

- **The Claude meta layer is dev-only, and staying that way is a decision.**
  Prod is the container, which has no `claude` binary and no `~/.claude`.
  Leaving `CLAUDE_ADMIN_CHAT_IDS` out of `.env.prod` is what keeps the layer
  off, and off is a supported state rather than a misconfiguration — the
  recording half runs exactly as it did before. Turning it on in prod is not a
  config change: it needs a credential mount and an image carrying the CLI,
  neither of which exists.

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

Every message ends at the same router, and a stored verdict decides what
happens there. How the verdict is reached is where the two arms differ: `/q`
asserts one, everything else is classified.

```
Telegram message
  → Dispatcher (aiogram)
  → populate_chat_data middleware   # injects: session, chat, config
  ├─ handlers/query.py  ask         # Command("q"), registered first
  │    store.upsert_message, COMMIT, verdict=question — no classifier call
  └─ handlers/handlers.py  record   # the filterless catch-all: everything else
       store.upsert_message, COMMIT
       classify.verdict_for         # one cheap call, after the commit
  → bot/routing.py
      fact     → 💔, enters the extraction tail
      question → answered in words; a refusal is handed to Claude instead
      talk     → the meta layer: queued, 👀 when the turn starts
      system   → stored, and nothing else
```

Asking no longer needs a command — the classifier takes a plain question, and
`/q` survives only as the override for when it gets one wrong. `presumed`
answers `question` for `/q` as well, so the two arms agree about what a `/q`
message is; nothing in the code enforces that agreement, and `verdict_for`
genuinely never runs for a `/q`. If you are ever debugging «why was my `/q`
message not classified» — it was not, and it was never meant to be. From
`routing.py` on the question arm is one path either way, and it is still the
only path that parses anything:

```
a question, with or without /q
  → bot/routing.py                  # the question arm
  → bot/answering.py  answer_for
      → extract.run                 # the whole unparsed tail, in one call
      → answer.spec_for             # question → a closed query spec
      → query.run                   # the spec → SQL → numbers
      → answer.render               # numbers → one or two sentences
  → bot/outbound.py  say            # send it, and store what was sent
```

`answer_for` returns `None` rather than prose when `spec_for` cannot express
the question, and that `None` is what makes a refusal a hand-off:

```
talk — or a question the closed set of aggregates cannot answer
  → bot/routing.py                  # hand_over, injected; nothing imports meta
  → telegrind/meta/                 # turn key → queue → `claude -p` → the text
  → bot/outbound.py  say            # a reply to the message that caused it
```

So there *is* a reply on ingest now, on two of the four arms. A `fact` gets a
reaction instead, and that bubble is still the delete gesture: 💔 means
«understood as a fact, tapping this removes it». 👀 means «handed to Claude»
and is deliberately not a heart, because a handed-over message has no facts to
delete. Everything else stays bare, which is what makes the queue legible on
screen — the messages with no bubble are the ones not yet seen:

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

**`telegrind/bot/handlers/handlers.py`** — ingestion, in two transactions with
the model call outside both. One filterless catch-all — no `COMMAND_LIKE`
filter any more — commits the row as a `fact` first, because that is what
shipped before the classifier existed and it is the invariant; only then does
`classify.verdict_for` run, and a second short transaction refines the verdict.
aiogram advances the polling offset as it dispatches, so an update lost inside
a handler is never redelivered: classifying before the commit would be a
model-call-wide window in which a message is silently dropped. Do not fold the
two back into one. `record_edited` honours the same rule in the mirror order —
read, classify, write — and an edit advances the receipt along `RECEIPT_CYCLE`
(💔 → ❤‍🔥 → 💘), which is the only signal that the bot noticed it. 👀 is the
point of no return: a message already handed to Claude is still overwritten,
but nothing downstream reacts to it.

**`telegrind/bot/handlers/query.py`** — `/q`, and nothing else. The only
handler registered ahead of the filterless catch-all, and the only place a
message's *first* verdict is set without asking `classify`: it stores the
message as a `question` and then calls the same `route` a classified question
reaches, so there is no second answering path in here. It clears `receipt_emoji` rather
than setting one, because a question carries no facts and 💔 would promise a
delete gesture that does nothing. `handlers` is imported inside the function
body, not at module scope — importing it here would register the catch-all
before this file's own decorator ran, and `ask` would be dead code behind it.

**`telegrind/classify.py`** — what a message is, and therefore what happens to
it. Four verdicts (`fact`, `question`, `talk`, `system`), and this is the only
place one is *derived* from a message: the slash rule lives in `presumed`
rather than in an aiogram filter, because two rules that can disagree about
whether a message is a command is a bug found in production, and the
`COMMAND_LIKE` filter that was that second rule is gone. Two verdicts still
reach a row without passing through here, and both are assertions rather than
derivations: `/q`'s own handler writes `question` — `presumed` answers
`question` for `/q` too, so they say the same thing, and keeping them saying
it is an obligation on both rather than something the code checks — and
`record_edited` re-writes the *previous* verdict unchanged when the message
was already handed to Claude, because nothing downstream may react to that
edit. `presumed` also answers `fact` for a message with
nothing readable, so no call is spent per sticker. Every failure
shape — a raising call, an unknown verdict, a missing key, a response that is
not a dict — degrades to `fact` explicitly; `verdict_for` never raises,
because the invariant that nothing written is lost must not come to depend on
a model call returning.

**`telegrind/bot/routing.py`** — the one place the four arms meet, and the seam
the meta layer plugs into. `hand_over` is a parameter, so nothing here imports
`telegrind.meta` and removing the conversation half is deleting one argument at
the call site. The question arm reads `message.text or message.caption` — the
same expression the classifier read, because whatever decides a verdict and
whatever answers it must read the same words — and sends the backlog notice in
a transaction of its own, outside the answering one. **All four arms run
inside one `try`**, and a failure says `BROKEN` in the chat: aiogram advances
the polling offset as it dispatches, so an exception escaping `route` is an
update that is never redelivered — and the message would be left wearing a
*bare* bubble, which the receipt vocabulary reads as «nothing yet, queued».
The guard writes nothing to the row on purpose: `receipt_emoji` stays `None`,
which is what keeps `record_edited`'s point-of-no-return gate open, so an edit
re-classifies and re-routes the message. The guard's own `say` is guarded too —
the outage that broke the arm can break the apology.

**`telegrind/bot/answering.py`** — question → numbers → prose, with no Telegram
in it, so it tests. Split out of `handlers/query.py` because it registers
nothing: importing a handler module is what registers its handlers, and
`routing.py` needs this half. `answer_for` returns `None`, not the refusal
text, when `answer.spec_for` cannot express the question — only a caller that
can tell a refusal from an answer can hand it to Claude instead.

**`telegrind/bot/outbound.py`** — the only way the bot speaks. Every outgoing
message is sent *and* stored, with `verdict=system` so it never enters the
extraction tail: a reply to something the bot said has to resolve to a row, or
the second hop of a session lookup has nothing to read. `ReplyParameters`, not
the `reply_to_message_id` deprecated at aiogram 3.27.

**`telegrind/bot/handlers/reactions.py`** — *any* user reaction tombstones the
message's facts; removing it restores them. Registering the observer is what
subscribes the `message_reaction` update type.

**`telegrind/store.py`** — the message and fact repository. `upsert_message`
appends on first sight and overwrites on edit, clearing `extracted_at` so the
next batch pass picks the message up again. A forwarded message is dated by its
origin, not by the forward. `unextracted_tail` and `context_before` are the
window a pass reads — and the tail now selects on `verdict`, not on
`extractable`; `replace_facts` diffs a message's facts by `seq`, so a
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

**`telegrind/bot/handlers/receipts.py`** — the receipt vocabulary: the cycle,
`HANDED_OVER` (👀), `acknowledge` and `clear_receipt`. Split out of
`handlers.py` because it registers nothing — `routing.py` needs `RECEIPT_EMOJI`
as a default, and importing it from `handlers.py` would register the catch-all
at a moment of its own choosing. 👀 is deliberately not a fourth heart: every
emoji in `RECEIPT_CYCLE` means «tapping this deletes the facts on this
message», and a handed-over message has none.

**`telegrind/meta/`** — the Claude half: a handler, not a second process. It
sees the update, owns the conversation, spawns `claude -p` and sends the reply.
It is a **guest** — nothing under it imports anything of the host's at all;
it takes a `MetaConfig`, a sessionmaker and four callables, and
`bot/meta_wiring.py` is the only file where the two halves touch. That is
asserted by `tests/test_meta_layer.py`, which walks the package with `ast`,
and not by naming three modules in prose: the boundary was breached by a
fourth — `runtime.py` importing the host's `llm` for `META_SYSTEM`, which
now lives in `runtime.py` itself — and every prose statement of the rule,
here and in the plan's Global Constraints, was narrowly true while it was. `sessions.py` derives a session id from the
message a turn answers
(`uuid5` over `chat_id:message_id`) and resolves which turn a reply continues
in at most two hops, so the session graph cannot drift from what Telegram shows
the user. `runtime.py` spawns one process per turn with `--resume <base>
--fork-session --session-id <new>`: forking is what keeps the derived id
self-consistent past the third turn of a thread, and it is full Claude Code —
the same CLAUDE.md, skills, hooks and MCP servers the user has in the TUI.
`run_turn` never raises; 👀 is a promise, and a crashed or hung process breaks
it silently unless the failure is said out loud. `_spawn` starts the turn in a
session of its own with `stdin=DEVNULL`, and a timeout kills the **process
group**, not the child: `claude` is a node process that spawns its own tools
and MCP servers, and `proc.kill()` leaves those running after the turn was
given up on. No test can see the orphans — nothing here may start a process —
so the two keywords are asserted against a fake and the survival itself is the
dev walk's to confirm.

**`telegrind/meta/queue.py`** — one turn at a time per session key, and the
rest queue. Same key, wait; different key, run now, because different fork
points are threads the user deliberately split. Two subtleties worth not
undoing. **A failed turn is retried cold only when `exit_code is not None and
exit_code > 0`** — only a process that ran to completion and exited positive
can be a `--resume` naming a session that was never written. A timeout or a
spawn failure has no exit code and is not about the session; a zero exit means
the CLI loaded the session and answered, however badly; and a *negative* code
is a signal kill, most plausibly the OOM killer, where a cold retry doubles the
memory pressure that caused it. It is deliberately not a substring match on the
CLI's wording — that text is truncated to 400 characters and is not a contract.
**And the same message handed over twice is closed at both ends,
asymmetrically:** the batch builder collapses it keeping the *last* text,
because a prompt not yet spawned can still be corrected; `submit` drops it
keeping the *first*, because one already with the model cannot. So an edit
arriving mid-turn is silently dropped — 👀 is lit, the hand-off still returns
True, and the only trace is a log line.

**`telegrind/bot/meta_wiring.py`** — those four callables, and nothing more.
Deleting this file is what separating the conversation half later means. The
one to understand is `claim`: it writes 👀 **to the row** at hand-off time,
before `submit` and therefore before `hand_over` returns, while the bubble goes
on screen later, when the turn actually starts. The column and the bubble are
two different promises made at two different moments — the column is the point
of no return an edit reads, and it cannot wait for the bubble, because a
message queued behind a running turn is not marked for the length of that turn.
The price is real and accepted: **an edit to a message that is queued but not
yet started is stored and never reaches Claude.** Every function here reads
inside its own `session.begin()`, for the reason below.

**`telegrind/bot/setup.py`** — the composition root, and the only place
handlers are registered. Registration is subscription: importing a handler
module registers its handlers, and aiogram derives `allowed_updates` from the
observers that have them — so dropping the `reactions` import would
unsubscribe `message_reaction` and kill the delete gesture with a green suite.
Order matters within an observer, and `handlers` ends in a filterless
catch-all, so `query` is imported first or `/q` is dead code behind it.
`attach_meta` builds the `MetaConfig` from the environment and is split out so
it can be called more than once per process, which is what makes the wiring
testable.

**`telegrind/coerce.py`** — the write boundary for a fact field. A value that
parses becomes a real JSON number, so `(fields->>'amount')::numeric` cannot
fail the whole query; one that does not stays text and simply never aggregates.
`to_instant` resolves a date against the **message's own** timestamp, in the
chat's timezone.

**`telegrind/config.py`** — `ChatConfig`, the timezone offset and default
currency, read off the `chat` row.

**`telegrind/models.py`** — `Chat`, `File`, `LoggedMessage` (table `message`)
and `Fact`. `LoggedMessage.verdict` is one of four strings and is never null —
null would mean «not classified», «the classifier failed» and «not a fact» at
once, which is undebuggable exactly when it misroutes. A fact is service
columns plus a JSONB `fields`: only `kind` and
`at` are promoted out, because every query filters on both. `deleted_at` is a
tombstone, and the uniqueness on `(message_pk, seq)` is a *partial* index so a
tombstoned fact does not collide with the row that replaces it.

**`telegrind/llm.py`** — the client, the model names, the two call helpers
(`use_tool` forces a tool call; `say` returns prose) and the prompts.
`EXTRACTION_RULES` is accumulated judgement about real messages, composed into
`EXTRACT_SYSTEM` with the observed taxonomy. `CLASSIFY_SYSTEM` and
`CLASSIFY_TOOL` are the verdict call — the tool's enum offers three values, not
four, because `system` is `presumed`'s to decide and never the model's — and
both of its tie-breaks lean the same way: towards `fact` when unsure of the
kind, towards `question` when unsure between asking and talking, because a
question the database cannot answer falls through to Claude anyway.
`META_SYSTEM` is **not** here — it is argv for a subprocess, not an
Anthropic-API call, and it lives in `telegrind/meta/runtime.py` beside the
command line that carries it. `PROMPT_VERSION` is extraction's
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
And one that a green gate will not show you: a **malformed** `# noqa` — prose
that merely begins that way, like `# noqa: this is not a real suppression` —
is reported as a `warning:` line, and `ruff check` then prints
*All checks passed!* and exits 0 anyway. Measured 2026-09-12. So the
suppression is silently absent and the gate says nothing is wrong; if you
write a comment that starts with `# noqa`, read the lines above the summary,
not just the exit code.
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
Session*. The rule that follows from it, and the one to apply rather than count
the scars: **every read wraps in its own `session.begin()`**, including a read
whose only purpose is to fetch a row for a later write — `meta_wiring.py`
states it once for the whole file and again at `speak`, and `routing.py`'s
question arm and `handlers.record_edited` state it at the site. Not
`bot/handlers/query.py`: the comment lived there until the answering path
moved into `routing.py` and `answering.py`, and it moved with it.
The suite cannot catch it: this repo's hand-written fake sessions yield from
`begin()` unconditionally, so only reading the code or running against a real
`Session` finds it.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

**The suite cannot see the meta layer at all, and that is by design.** No test
may reach a subprocess, the network or a real `claude` binary, so everything
`telegrind/meta/` does with a process is asserted against a fake `spawn`. Two
other things are invisible for the same structural reason. `expire_on_commit=
False` in `main.py` is load-bearing — `record` commits the row and then hands
it to `routing.py`, which reads `row.verdict` — and no test can catch its
removal, because a fake session has no commit that expires anything; the
likeliest live traceback is `MissingGreenlet` at an attribute access, which
mentions neither commits nor transactions. And `--fork-session` carrying memory
across turns rests on one measurement, not on a test. Read the code for these,
or walk it on the dev bot.

**`# noqa: I001` in `bot/setup.py` is not about the import cycle.** `I001` is
isort's *unsorted-block* rule, and the suppression exists because the import
order in `setup_dispatcher` is registration order and therefore match order:
`query` must be imported before `handlers`, which ends in a filterless
catch-all, and alphabetising them would silently make `/q`'s own handler dead
code. The import *cycle* is a separate hazard with a separate home — the
comment in `telegrind/bot/handlers/__init__.py`, which explains why that
package registers nothing. Both are real, they are not the same thing, and a
report in this very plan got them confused.

**The prompt goes into `claude -p`'s argv after a `--`, and that is a security
boundary.** A `talk` message is arbitrary user text handed straight to a
subprocess running `--permission-mode bypassPermissions`. Measured 2026-09-12
against claude 2.1.269: without the separator, a message of `--version` made
the CLI print its own version and the prompt never reached the model; with it,
the same argv parsed as text. It is one list element in `runtime.argv` and it
looks like noise.

**`CLAUDE_CWD` unset points that subprocess at the checkout you are working
in.** `MetaConfig.from_env` falls back to `Path.cwd()`, so a bot started the
documented way — `uv run python main.py` from the repo root — runs full Claude
Code under `bypassPermissions` in the live tree, uncommitted work and all, on
the first `talk` message. The spec is emphatic that Claude never edits the live
tree and says it about *production*; dev is where this runs first, and nothing
carried the rule across. **Point `CLAUDE_CWD` at a scratch worktree before the
first talk message** (`git worktree add /tmp/claude-sandbox HEAD`), which is
what `.env.dist` and the dev walk's step 0.1 both say. There is deliberately no
guard that refuses to start: the value has a legitimate use and a bot that will
not boot is a worse failure than one that needs a line of config.

**The allowlist is on `chat_id`, not `from_user.id`.** In a private chat the
two are the same number, which is why it has never mattered — but an admin id
that names a *group* puts every member of that group at the `claude -p` prompt.

**Model-authored text is sent with `parse_mode=None`, everything else keeps the
default.** The bot-wide default is HTML, so an answer carrying a bare `<` — a
comparison, a currency rendering, a line of code — comes back «can't parse
entities» and the *whole answer* is lost rather than sent plain. The override
belongs to text no human wrote, and there are exactly two sites:
`meta_wiring.speak`, and the one call in `routing.py` that sends whatever
`answer_for` returned. Everything else the bot says keeps the default — with
one harmless leak, which is that `answer_for` returns `EMPTY_QUESTION`, a
fixed string of ours, down that same path. Do not widen it further: our own
strings are markup-free, and the override is what marks a line as untrusted. The aiogram mechanics are in
`docs/telegram-bot-api.md`.

**The aiogram router is a module-level singleton, so a test asserting
registration order can be reading pytest's collection order instead.** Another
test module that imports the handlers at module scope and sorts earlier warms
the package first, and the assertion then passes or fails for reasons that have
nothing to do with the code under test. Clear the observers and drop the
handler modules from `sys.modules` — **the package too, and first**, or
`from .handlers import query` resolves off the stale package attribute and
registers nothing. Prove such a test discriminates by breaking the wiring both
ways before believing it.

**A test that passes against deliberately broken code is an ordinary outcome,
not a freak one.** The plan that built the meta layer produced three of them:
two caught by the implementer who had just written them, one by a reviewer
afterwards. That is the whole argument for the ritual: revert the fix, run the named test, and watch it fail *for the
reason you expect*. A test that fails for some other reason (an import error,
a fake that never reaches the assertion, a fixture whose conditions the bug
cannot occur under) has not discriminated either, and it will go on passing
after the fix rots. Two of the three were caught exactly this way and the
third was not caught at all until someone else read it. It costs a minute per
test. The router-singleton trap above is one instance; the practice is
general.

**Bump `PROMPT_VERSION` only when the prompt changed meaning.** It is stamped on
every extracted fact and is what a later re-extraction pass uses to find facts
produced by an older prompt; a gratuitous bump invalidates the eval baseline and
stamps mismatched versions on facts written during live testing.
<!-- src: telegrind 35574a2 | 2026-09-12 -->
