# Project memory — telegrind

Durable facts about *this repo* that the code and git history do not state.
One bullet per fact, under a topical heading. No secrets.

## Product invariants

- **"Ничего из написанного не теряется"** is the product claim, not a slogan:
  every handler writes the message row to Postgres *unconditionally*, and only
  then projects to Sheets if a workbook is linked. Declining to extract
  (unknown command, voice, `??`) is never licence to drop the message.
- **The workbook is an optional projection**, not the store — since
  `ef80f31` (2026-09). There is no onboarding gate and no FSM; `/link` is a
  command the user reaches for, not a wall they pass.

## Telegram Bot API — decisions already made (see `docs/telegram-bot-api.md`)

- **Checklists cannot be the wishlist UI.** `sendChecklist` /
  `editMessageChecklist` require `business_connection_id: str`, not optional.
  Dead for a plain bot — do not re-propose.
- **The Bot API does not transcribe voice notes.** Phase 3 (`/retranscribe`)
  needs an external ASR; Telegram contributes `getFile` only, capped at 20 MB.
- **Registering a handler is what subscribes to an update type.** aiogram
  derives `allowed_updates` from the observers that have handlers, so reading
  users' reactions back requires a `message_reaction` handler — there is no
  separate config knob.
- **A bot DOES receive `message_reaction` in a private chat.** The API docs say
  "the bot must be an administrator in the chat"; in a private chat that
  condition is vacuous. Measured 2026-09-11 against `@assinstantbot`: setting a
  reaction gave `old_reaction: [] → new_reaction: [emoji]`, removing it gave a
  separate update with an empty `new_reaction`. So reaction-driven UX is
  reachable, and removal is observable too — do not re-derive this from the
  docs, they do not settle it.
- **`old_reaction`/`new_reaction` are that one user's reactions, not the
  message's total.** A bot can react to the user's own message, its reaction
  never appears in those lists, and it generates no update — so the bot can
  place a reaction as an affordance and the user taps that existing bubble to
  react with one tap. Measured 2026-09-11.
- **One reaction per message, both sides.** A bot setting two gets
  `REACTIONS_TOO_MANY`; the schema caps bots at one and a bot cannot be Premium.
  A non-premium user also holds one at a time. So reactions are a radio button,
  never a row of independent switches — do not re-propose multi-reaction UI.
- **Message deletion cannot be observed at all.** The only deletion update is
  `deleted_business_messages`, and `getMe` reports `can_connect_to_business:
  false` for this bot. Dead twice over.
- Pinned aiogram 3.27.0 = Bot API 9.6 (verified 2026-09-10). Bot API 10.x
  features (Rich Messages, ephemeral, guest mode) need a bump to 3.31.

## Known drift

- The `## Architecture` section of the root `CLAUDE.md` is stale as of
  2026-09-10: it still describes the `Outcome`/`Loan`/`Wish` `Sheet`
  subclasses and the `/start` onboarding FSM that `ef80f31` removed. The
  current shape is `llm.extract` → `projection.apply_changes` over
  `store`-owned `Message`/`Fact` rows.
- **`docs/` does not exist on `main` at all.** Verified 2026-09-17:
  `git cat-file -e main:docs/telegram-bot-api.md` and both design specs fail. So
  the root `CLAUDE.md`'s instruction to read `docs/telegram-bot-api.md` before
  touching the Telegram side, and this file's pointer to it, resolve to nothing
  in the main checkout — those documents live only on `metheoryt/v2`.
  <!-- src: telegrind c5a6b01 | 2026-09-17 -->
- **Of the branches the `CLAUDE.md` scope note names, only `metheoryt/v2`
  survives.** `metheoryt/dialogue-first` and `metheoryt/claude-meta-1-4` were
  deleted from origin on 2026-09-14 after `git merge-base --is-ancestor` proved
  both fully contained in `metheoryt/v2`; origin carries `main` and
  `metheoryt/v2` and nothing else. Both local refs are still here (2026-09-17)
  for no better reason than that nobody ran `git branch -d` after their Orca
  worktrees went away — only `main` and `v2` are checked out now.
  <!-- conflicts-with: "**Everything below describes the dialogue-first rewrite**, which lives on `metheoryt/dialogue-first` and has never been deployed" -->
  <!-- src: telegrind c5a6b01 | 2026-09-17 -->

## The workbook layer is gone (2026-09-11)

- **There is no Sheets projection any more.** `248fe9d` deleted the workbook
  layer whole — no `gspread`, no `projection` module, no `_config` /
  `_categories` worksheets, and no `/link`, `/import`, `/rebuild`, `/reload` or
  `/unlink`. `/q` is the only command the router registers. Storage is
  unconditional into Postgres and stops there: the invariant survived the
  deletion, the projection did not. Anything written about registries, header
  ranges, `origin="imported"` facts or `sheet_key` describes a shape that no
  longer exists.
  <!-- conflicts-with: "every handler writes the message row to Postgres *unconditionally*, and only then projects to Sheets if a workbook is linked" -->
  <!-- conflicts-with: "**The workbook is an optional projection**, not the store — since `ef80f31` (2026-09). There is no onboarding gate and no FSM; `/link` is a command the user reaches for, not a wall they pass." -->
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **`Chat.sheet_url` outlived the layer that used it, on purpose.** The
  `dialogue_first` migration recreated `fact` without its spreadsheet
  coordinates but left this column on `chat`, and `tests/test_models.py` still
  asserts it. Nothing in `telegrind/` reads it. It stays because prod
  `telegrind_pgdata` holds live rows with real values and the retired workbook
  is still the only copy of the pre-bot history — do not clear it as dead code
  before that history is imported.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **The root `CLAUDE.md` Architecture section describes the code that exists.**
  It walks `middleware` → `handlers` → `store` → `taxonomy` → `extract` →
  `query` → `answer` → `receipts` → `coerce` → `config` → `models` → `llm`.
  There is no `llm.extract` and no `projection.apply_changes` anywhere in the
  tree.
  <!-- conflicts-with: "The `## Architecture` section of the root `CLAUDE.md` is stale as of 2026-09-10: it still describes the `Outcome`/`Loan`/`Wish` `Sheet` subclasses and the `/start` onboarding FSM that `ef80f31` removed. The current shape is `llm.extract` → `projection.apply_changes` over `store`-owned `Message`/`Fact` rows." -->
  <!-- src: telegrind 35574a2 | 2026-09-12 -->

## Deployment and the two bots

- **Nothing auto-deploys; a push to `main` deploys nothing.** The poll-and-build
  pipeline was a PowerShell Scheduled Task on the retired `server` box, and
  latitude has neither `pwsh` nor a port of it. Every deploy is a manual
  `git pull` plus `docker compose -f compose.prod.yml up -d --build` on
  latitude. The root `README.md` still says the opposite and should be corrected
  the next time someone edits it.
  <!-- conflicts-with: "Deployed on the homeserver by the `vps` repo poll-and-build pipeline — pushing to `main` is the deploy." -->
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **Dev and prod are different bots on different machines.** The dev `.env`
  token is `@assinstantbot`; production is `@telegrindbot` and runs on latitude,
  not on this laptop. Verified with `getMe` on each token plus `docker ps` on
  both boxes, 2026-09-11, after a plan document had asserted a two-poller
  collision for weeks. Stopping the dev bot is tidiness, never a safety step.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **`entrypoint.sh` runs `alembic upgrade head` before `exec python main.py`,**
  so every container start applies pending migrations and a bad migration
  surfaces as a startup failure rather than as silent drift. The consequence for
  rollback: by the time a health check fails the schema has already moved, so a
  bare `git reset --hard` leaves old code facing a new schema. Downgrade first,
  then reset, then restart.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->

- **Prod is NOT the dialogue-first code — it is the 2026-08-01 marvin/workbook
  build.** Measured 2026-09-12: image `telegrind-bot:local` created 2026-08-01,
  container env still carries `MARVIN_AGENT_MODEL` and
  `GOOGLE_SERVICE_ACCOUNT_FILE`, `src` sits at `ffce27a` on `main`, and the prod
  database (`postgres`, not `telegrind`) holds only `alembic_version`, `chat`
  and `file` at revision `2700e0b3a8b6` — no `message`, no `fact`, zero logged
  messages. `getWebhookInfo` shows `allowed_updates` without `message_reaction`.
  So anything CLAUDE.md describes — the 💔 receipt, tombstone-by-reaction, `/q`
  — is absent from what actually runs, and every prod reply goes through marvin
  to Anthropic. `2700e0b3a8b6` IS in the current migration graph
  (`20260424015753_unique_chat_id`), so a real deploy is a plain
  `alembic upgrade head`, not a data migration.
  <!-- src: latitude prod inspection | 2026-09-12 -->
- **A dead `ANTHROPIC_API_KEY` presents as "the bot stopped reacting", not as an
  error to the user.** On 2026-09-12 every message raised
  `ModelHTTPError 401 authentication_error` deep inside marvin/pydantic-ai and
  the user saw only silence; Telegram was healthy throughout (`pending_update_count`
  0, no webhook), so the polling side is a red herring. Check the key against
  `https://api.anthropic.com/v1/models` directly before reading any traceback.
  Fix: replace the value in `vps/homeserver/telegrind/.env.prod` and
  `docker compose -f compose.prod.yml up -d` — **`docker restart` reuses the
  baked-in env and the new key never loads.**
  <!-- src: latitude prod inspection | 2026-09-12 -->
- **v2 runs in production BESIDE v1, and that is what forbids merging
  `metheoryt/v2` into `main`.** Since 2026-09-15 latitude carries a second stack
  at `~/my/vps/homeserver/telegrind-v2/`: compose project `telegrind-v2`, its own
  network, volume `telegrind-v2_pgdata`, image tag `telegrind-bot:v2` (v1 builds
  `telegrind-bot:local`), bot `@teamlegrambot`, and a `src` clone that tracks
  **`metheoryt/v2`, not `main`**. One image tag or one branch shared between them
  would mean v1's next restart running v2's migrations against v1's live
  database — `entrypoint.sh` applies `alembic upgrade head` before the bot
  starts, unattended. So while both bots run, v2 stays off `main`.
  <!-- src: telegrind c26a7cb | 2026-09-17 -->
- **Both stacks fail their first container start on a cold boot, by design of
  the entrypoint rather than by fault.** `alembic upgrade head` runs before
  postgres finishes starting, the bot exits, and `restart: unless-stopped`
  brings it back (`RestartCount=1`). It is a healthcheck on postgres plus
  `depends_on: condition: service_healthy` away from being clean — until then, a
  single restart in the logs after a reboot is not evidence of a defect.
  <!-- src: telegrind c26a7cb | 2026-09-17 -->
- **v1's database never held any history at all, so "migrate the data from v1"
  is not a database job.** Measured on latitude 2026-09-16: `chat` 62 rows (12
  with a `sheet_url`), `file` 1 row, and no `message`/`fact` tables — v1 never
  stored text, it projected into the workbook. The only copies of the real
  history are the Telegram Desktop export of the chat and the old Google sheet.
  v2's own volume is not in restic yet.
  <!-- src: telegrind c26a7cb | 2026-09-17 -->

## Extraction — what the corpus taught

- **The observed taxonomy is a magnet, not just a brake, and a catch-all kind
  named `facts` means "anything".** With the reuse instruction «переиспользуй
  существующий kind, если подходит», "if it fits" reads as "it always fits" for
  a kind that means anything, so once one measurement lands in the dump every
  later one follows it. Measured 2026-09-11 with the same prompt and the same
  message: an EMPTY vocabulary produced `health {"weight": 82.5}`, the live
  vocabulary containing `facts` produced `facts`. The fix is to carve the
  catch-all explicitly out of the reuse rule in the prompt.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **Do not tighten the leading-amount rule.** The hedge in "when a message opens
  with an amount of money AND NO OTHER CATEGORY FITS IT, it is an `expense`" is
  load-bearing. Two stronger wordings were measured and both made things worse:
  "begins with a bare number … never `facts`" dragged future-tense intentions
  («12 октября куплю подарок за 20000») out of `wish` and into the catch-all,
  and adding an explicit leading-date carve-out took «в пятницу заплачу» down
  with it — naming cases inside a rule is what attracts them. Re-run the
  future-tense fixtures before touching this wording.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **A new kind's name is unstable until one locks in.** Probing the same message
  repeatedly against an empty vocabulary coined `health`, `weight` and
  `blood_pressure` across runs. The brake only bites once a name exists, so the
  first facts of a category are where drift is decided — and a bulk history
  import, which coins many kinds at once against a thin vocabulary, is the
  moment to watch rather than a later steady state.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **An A/B against another extraction model must run on a scratch chat.** The
  code change is trivial — everything goes through `llm.use_tool` / `llm.say`,
  both injected — but `taxonomy.observed()` feeds the chat's own vocabulary back
  into every later prompt, so a challenger that coins one synonym splits every
  future sum AND poisons the prompt for later passes that go back to the good
  model. The quality gate cannot see kind drift at all, so it would pass such a
  model.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **A truncated extraction reply is indistinguishable from a successful pass, so
  the pass size is a correctness bound and not a throughput knob.** `extract._pass`
  makes ONE model call for the whole batch against `llm.MAX_TOKENS = 2048`,
  `llm.use_tool` never inspects `response.stop_reason`, and `_pass` marks the
  entire tail extracted on any return that does not raise. An oversized batch
  therefore skips messages permanently, with no error, no `extract_error` and no
  trace — which is why a bulk backfill is run in passes of ~20 and why the
  runbook is stop the bot → import → extract until the tail is empty → start.
  <!-- src: telegrind 591ed41 | 2026-09-17 -->
- **A kind absent from the FIRST pass can never be coined later.**
  `taxonomy.observed()` shows the extractor only the kinds and field names
  already present in `fact`, so the vocabulary can only ever grow from what a
  pass already produced. Measured over the whole 3915-message corpus on dev
  (2026-09-17, 194 passes, 3856 facts, 0 failures): `expense` 3624, `loan` 184,
  catch-all `facts` 59, `weight` 1 — catch-all 1.5% — and **`wish` was never
  coined at all**: all six «хочу …» messages landed in the catch-all. ~8s per
  pass, $1–2 for the corpus on Haiku.
  <!-- src: telegrind 591ed41 | 2026-09-17 -->
- **Importing structured rows teaches the live bot, silently.** The same
  `taxonomy` readback means an import's field names enter the prompt: bringing
  the v1 sheet's `category` and `necessity` in (2129 labelled rows) makes the
  extractor start stamping both onto new expenses nobody asked it to. Decide
  that as behaviour, not as an import detail — and name imported fields exactly
  as the live extractor already writes them (`amount`, `currency`, `comment`,
  `person`, `when`, `due`, read out of `fact`), or imported and extracted rows
  will not sum in one query.
  <!-- src: telegrind 591ed41 | 2026-09-17 -->
- **`fact.prompt_version` is a write-only column.** Measured 2026-09-17: it is
  written in three places (`extract.py:284`, `extract.py:287` via
  `store.mark_extracted`, `store.py:332`) and read by nothing in `telegrind/`.
  Whatever a re-extraction pass would need in order to find facts made by an
  older prompt, it does not get from this column today.
  <!-- conflicts-with: "`PROMPT_VERSION` is extraction's and is what `fact.prompt_version` records" -->
  <!-- conflicts-with: "It is stamped on every extracted fact and is what a later re-extraction pass uses to find facts produced by an older prompt" -->
  <!-- src: telegrind 591ed41 | 2026-09-17 -->

## Ingest invariants that are easy to break

- **A message with nothing readable is a third state, not a failure.** A
  caption-less photo, a sticker or a location is stored with both `extracted_at`
  and `extract_error` null — not yet parsed, which is distinct from both
  "parsed, yielded nothing" and "failed". It is never sent to the model and
  never enters the window budget, because `_has_content()` gates the extraction
  tail and the announced backlog count alike.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **A pass-level complaint must not be written into `extract_error`.** That
  column means "this message's own extraction failed"; smearing a pass-level
  note (a fact attributed to a message outside the window, say) across every row
  in the tail makes the countable «N сообщений не удалось разобрать» lie. Count
  it in the pass report and surface it in the `/q` reply instead.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **Probe a receipt emoji against the live API before hardcoding it.**
  `acknowledge` deliberately swallows `setMessageReaction` failures — the row is
  already committed, so a Telegram error costs a visual cue and nothing else —
  which means an emoji Telegram rejects fails *silently* and the reaction simply
  never appears.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->
- **Driving a manual walkthrough from inside the chat works, with one catch.** A
  scratch `sendMessage` script outbound plus a database poller inbound saves the
  operator switching surfaces, but the operator's own replies are ordinary chat
  messages: they enter the extraction tail, get parsed and add rows. Harmless to
  sums when they carry no number, confusing to an announced backlog count.
  <!-- src: telegrind 35574a2 | 2026-09-12 -->

## Routing — where the shipped code and the meta-layer spec part company

- **An edit preserves the row's previous verdict; it does not re-derive one.**
  The design doc says an edited message is re-classified, and the code
  deliberately does not do that yet: re-deriving from the `/` prefix on the
  edit path would flip a stored `/q` row from `question` back to `fact` the
  first time the user fixed a typo in their own question. A row with no
  previous sighting falls through to the first-sighting default. Do not read
  the spec's re-classification paragraph as a description of what runs.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **`extractable` is still on `message` on purpose, and dropping it is a hand
  step.** The verdict migration is the expand half of an expand/contract pair:
  it adds `verdict` non-null with a server default, backfills it from what the
  old flag meant (`/q%` → `question`, every other unextractable row →
  `system`), and leaves `extractable` in place and still written. That is what
  keeps the downgrade a plain `drop_column` and the spec's rollback reachable.
  The contract step — dropping the column and its writers — is taken by hand
  once the verdict has held, and nothing in the migration graph will prompt for
  it.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **The reply chain outranks the classifier, and it had to, because an empty
  ledger answer is indistinguishable from a misroute.** Measured live
  2026-09-13: «а последний коммит какой», said as a reply to a bot answer, was
  classified `question`, went to the SQL engine, and came back «По этому вопросу
  записей нет» — the fallback that hands a REFUSED question onward never fired,
  because nothing had refused. `store.turn_root` now resolves which turn a reply
  continues (one hop through the bot's own row, since Telegram does not nest
  replies) and passes that verdict to the classifier as a REQUIRED argument.
  Two deliberate limits: a command still outranks the chain (`/q` as a reply
  still asks the ledger), and only `talk` is contagious.
  <!-- src: telegrind 6c679a1 | 2026-09-17 -->
- **An edit now re-classifies and re-routes, and a question the spec engine
  cannot express is simply refused.** Cutting the hand-over seam (`944dab3`)
  removed the `HANDED_OVER` gate that used to stop an edited row from being
  re-routed, and removed the arm that let a refused question fall through to
  Claude. The fact/question boundary used to be soft — a misclassification cost
  a second of latency; now it costs the answer.
  <!-- conflicts-with: "An edit preserves the row's previous verdict; it does not re-derive one." -->
  <!-- src: telegrind 944dab3 | 2026-09-17 -->

## Environment and the test harness

- **A bare `uv sync` destroys the venv it was replacing.** The repo carries no
  `.python-version` and only `requires-python = ">=3.14"`, so `uv` picks the
  freethreaded CPython 3.14.7 it manages; `psycopg-binary` 3.3.3 publishes no
  `cp314t` wheel, the resolve fails, and `uv` has already removed the old
  environment by then. This bites every fresh clone and every new worktree.
  The working form is `uv sync -p /usr/bin/python3.14`.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **The stored `raw` JSONB carries the key `from_user`, never the Bot API's
  `from`.** aiogram declares `from_user: User | None = Field(None, alias="from")`
  and `store.upsert_message` dumps with `model_dump(mode="json")` and no
  `by_alias=True`, so the alias never survives into the column. Anything
  identifying a bot-sent row must read `raw["from_user"]["is_bot"]`; reading
  `raw["from"]` inverts the answer and leaves the suite green, because the
  fakes dump the same way.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **`setup_dispatcher()` can only run once per process, so a registration test
  reads pytest's collection order unless it resets first.** `router` and `dp`
  are module-level singletons and a second `dp.include_router(router)` raises
  *Router is already attached*. A test that asserts handler order therefore has
  to clear the observers and drop the handler modules from `sys.modules` — the
  package entry first, or `from .handlers import query` resolves off the stale
  package attribute and registers nothing at all.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **Registration order is match order, and a dropped import silently
  unsubscribes an update type.** `dp.resolve_used_update_types()` derives
  `allowed_updates` from whichever observers happen to have handlers, so losing
  the `reactions` import removes `message_reaction` and with it the bot's only
  delete gesture — with the suite still green, unless a test asserts both the
  observer order and the resolved update types. Reordering the imports in
  `setup_dispatcher()` changes which handler claims a message.
  <!-- src: telegrind db9de98 | 2026-09-12 -->

## The Claude meta layer — hazards to carry into the merge

- **`CLAUDE_CWD` unset means the bot spawns Claude Code inside the live
  checkout.** The meta-layer handler falls back to the bot's own working
  directory, so the first conversational message runs full Claude Code — this
  repo's `CLAUDE.md`, skills, hooks and MCP servers — under
  `--permission-mode bypassPermissions --permission-prompts none` against
  uncommitted work. Point it at a scratch `git worktree` before starting the
  bot in development. The allowlist is keyed on `chat_id` rather than
  `from_user.id`, so an admin id that names a group hands every member of that
  group a Claude subprocess.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **The multi-turn design rests on two claims measured exactly once, with no
  test behind either.** That `--resume <base> --fork-session --session-id <new>`
  loads the parent's history and writes the transcript under the NEW id rather
  than back into the base; and that a dead `--resume` target exits 1 before any
  model call (stderr `No conversation found with session ID: <uuid>`). If the
  first is wrong the bot answers every turn as though the conversation had not
  happened and nothing in the suite notices. Treat both as assumptions until a
  live multi-turn walk retires them. A related consequence of the same design:
  because each turn mints a fresh id and chains to the previous turn, every
  outbound reply must target the message that triggered *that* turn, never a
  fixed anchor.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
- **The meta layer is OUT of telegrind as of 2026-09-14 — it is not merging, and
  the hazards below now describe another repository's problem.** The forcing
  fact is that a process cannot rebuild and restart itself: `docker compose up -d
  --build` destroys the container the turn is running in, and aiogram has already
  advanced the polling offset, so the deploy request dies as silence rather than
  as an error. The control bot is `~/my/cladaeb`, a host-side process with its own
  token, deliberately given root on latitude, gated by a `from_user.id` allowlist
  (never `chat_id` — an id naming a group hands root to every member). A second
  new repo, `~/my/aiogram-blackbox`, records what a bot saw and said to JSONL;
  the dependency direction is **apps → recorder**, and nothing about Claude may
  live in it.
  <!-- conflicts-with: "## The Claude meta layer — hazards to carry into the merge" -->
  <!-- src: telegrind 944dab3 | 2026-09-17 -->

<!-- KB refreshed against c5a6b01 on 2026-09-17 -->

## Vendor-API facts routed here from the 2026-09-12 shared proposals

The harvest tagged these `global`; `/memory-review` routed them to this repo on
2026-09-12 because they are domain facts, not fleet-wide ones. **Note:** the Google
Sheets rows describe the pre-dialogue-first layer deleted 2026-09-11 (`248fe9d`) —
they are kept because the vendor behaviour is true independently of telegrind, not
because that code still exists.

### `dateparser` (measured against 1.2, 2026-09-11)

- **`RELATIVE_BASE` must be the user's local wall clock, not UTC.** At 02:00 in a
  UTC+6 zone it is still the previous day in UTC, so a naive UTC base makes
  «сегодня» resolve one day behind what the writer meant.
- **`dateparser.parse` returns `None` — no date at all, not a wrong one — for every
  Russian `<day> <time-of-day>` compound**: «вчера вечером», «вчера утром»,
  «позавчера вечером», «в понедельник утром», «вчера днём», «вчера ночью», «сегодня
  вечером». The silent `None` means the phrase falls through to whatever fallback the
  caller supplies, so a relative date quietly becomes "now". Strip the time-of-day
  qualifier with a regex and re-parse the day alone.
- **A relative date parser is the wrong tool for a period BOUNDARY.** «август»
  through `dateparser` yields *this day* in August, not the month. Month ranges have
  to arrive as explicit ISO dates, half-open with the upper bound exclusive so
  nothing lands in two months.

### Google Drive / Sheets

- **A Google service account has no Drive of its own.** `about.get` returns
  `storageQuota {limit: "0", usage: "0"}`, and both `files.create` of a Google-native
  spreadsheet and `files.copy` fail 403 `storageQuotaExceeded`; creating inside a
  shared folder does not dodge it, because **quota follows the creator**. So a
  service-account bot can read and write any workbook shared with it forever but can
  never create or own one. The only way for a bot to create a spreadsheet is OAuth as
  a real user (the `drive.file` scope is non-sensitive, needs no verification, and its
  refresh token does not expire — but it can only touch files the app itself created,
  so importing a pre-existing workbook must happen BEFORE switching). A 403
  `SERVICE_DISABLED` is a different error: the Drive API is not enabled in the GCP
  project at all.
- **Sheets `values.append` places rows after the sheet's *data extent*, not after the
  last row of the range you declare.** On a worksheet carrying user formula columns
  filled down every row, that extent sits far below the range just cleared: measured
  on a real 3501-row workbook, a rebuild appended all 3501 rows starting at row 3505,
  leaving 3503 blank rows above and growing the grid from 3502 to 7005. Write at an
  explicit `A<row>` range instead, growing the grid when needed and never shrinking
  it. Separately, a worksheet created with `rows=1` makes any `A2:E` range a hard 400
  (`exceeds grid limits. Max rows: 1`), not a no-op — create with a real row allowance
  and guard clear-style operations with a `row_count <= 1` early return.

### The meta layer's derived session ids

- **A session id derived from the message being replied to does not survive three
  turns without `--fork-session`.** Trace: Claude answers M1 in `uuid5(M1)` and
  replies to M1; a user reply F1 to that answer resolves back to M1, and
  resume-in-place continues `uuid5(M1)`, whose answer replies to F1; the next user
  reply resolves to F1, and `uuid5(F1)` was never created. **Forking each turn into
  its own derived id is what makes the scheme self-consistent**, and it is one extra
  flag. (The general `claude -p` session-control facts went to `global.md`.)

## New sources — the `entry` cut (design of record from 2026-09-17)

- **Facts hang off an `entry`, not off a message, and that is a defence of the
  ingest path rather than a tidy-up.** A telegram update lost mid-handler is
  never redelivered, so `message` is the one table where a schema mistake is
  unrecoverable; every future source (a receipt, a bank statement, the v1 sheet)
  therefore adds rows to `entry` instead of columns to `message`. `entry` carries
  `(chat_pk, source, external_id)` unique — which is what makes a re-import
  update rather than duplicate — plus `verdict` and the four extraction-state
  columns, which move off `message` with it. Rejected alternatives, both
  measured: widening `message` itself (13 files, 43 references) and making
  `fact.message_pk` nullable (two classes of fact that `replace_facts`,
  `tombstone_facts`, `restore_facts` and the reaction handler would each have to
  learn).
  <!-- src: telegrind c26a7cb | 2026-09-17 -->
- **A fact carries no provenance of its own — `entry.source` is both the origin
  and the undo.** Everything one import wrote is `WHERE source = '<that source>'`,
  one statement, which is why no import-run table was needed when the import
  machinery was cut back to the schema alone. Stamping the source onto the fact
  as well was written into the spec and then removed: it is the second marker
  saying what the first already says, and this repo has now been burned twice by
  exactly that pair.
  <!-- src: telegrind c26a7cb | 2026-09-17 -->
- **The v1 workbook cannot be joined to the chat history.** Its column `#` is a
  running row counter (234…10457, no collision between sheets), not a Telegram
  `message_id` — v1's were seven digits. That premise was the whole foundation of
  `workbook_compare.py`, and `import_history.py` with it, so both were retired
  (`591ed41`); the chat export stays on disk and the code stays in git history.
  <!-- src: telegrind 591ed41 | 2026-09-17 -->
- **Two traps in a Telegram Desktop export, both silent.** In a private chat the
  top-level `id` is the INTERLOCUTOR — the bot — not your own `chat_id`, so
  deriving the chat from the file is how a whole history lands under the wrong
  chat; pass the id explicitly and refuse an export carrying a third `from_id`.
  And `edit_date` must be handed in as a raw int: `store.message_values` dumps
  with `mode="json"`, so a `datetime` lands in `raw` as a string where live
  ingest puts a number, and the entire "we go through aiogram so `raw` matches
  live by construction" argument quietly stops holding.
  <!-- src: telegrind 591ed41 | 2026-09-17 -->

## Worktrees, the dev stack, and the main checkout

- **Every worktree of this repo shares one dev postgres and one `.git`.** A
  migration run in one worktree moves the schema under every other checkout: when
  `extractable` was dropped it was verified up AND down against the live dev
  database (after `downgrade -1` the restored column disagreed with
  `verdict = 'fact'` on 0 of 41 rows) and the dev DB was then deliberately left at
  `97b074369b9a`, because the main checkout still wrote that column. For the same
  reason a bare `git stash` in a worktree reaches into a stack other sessions
  share.
  <!-- src: telegrind 895a615 | 2026-09-17 -->
- **A fresh worktree has no `.env` and no `.venv`, so alembic and pytest do not
  start there.** Copy or load the main checkout's `.env` — in-process with an
  explicit path, never `source` (the CRLF rule above), and `uv sync
  -p /usr/bin/python3.14`, never bare.
  <!-- src: telegrind 895a615 | 2026-09-17 -->
- **`/home/me/my/telegrind` is an archaeological copy, not a workspace.** It sits
  on `main`, which is the 2026-08 build prod v1 runs; all work happens in the
  Orca worktrees under `~/orca/workspaces/telegrind/`. `CLAUDE.md` and
  `.claude/memory/project.md` were carried onto `main` deliberately (`c5a6b01`)
  so the design of record and these facts travel with the repository rather than
  with one branch — telegrind tracks `project.md` on purpose, and dotfiles tracks
  no `project.md` at all.
  <!-- src: telegrind c5a6b01 | 2026-09-17 -->
