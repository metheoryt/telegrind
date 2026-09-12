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
  It walks the recording half (`middleware` → `handlers` → `store` →
  `taxonomy` → `extract` → `query` → `answer` → `receipts` → `coerce` →
  `config` → `models` → `llm`) and, since the meta layer landed, the
  classifying and conversing half too (`classify` → `bot/routing` →
  `bot/answering` → `bot/outbound` → `meta/` → `bot/meta_wiring` →
  `bot/setup`). There is no `llm.extract` and no `projection.apply_changes`
  anywhere in the tree. The stale-since-2026-09-10 note this replaces has been
  retired rather than corrected a third time.
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

## The Claude meta layer (2026-09-12)

- **`--fork-session` is load-bearing at build-order step 4, not step 5.** The
  spec puts forking in step 5 with the warm base; a derived session id without
  it does not survive three turns. Claude answers `M1` in `uuid5(M1)` and
  replies pointing at `M1`; the user replies to that with `F1`, the two-hop
  rule resolves to `M1`, and a resume-in-place run answers from `uuid5(M1)`
  again; the user replies to *that* with `F2`, the rule resolves to `F1` — and
  `uuid5(F1)` was never written, because resume-in-place never created it.
  Forking makes the derived id self-consistent and costs one flag. The warm
  base is genuinely step 5's, and this plan pays a cold session per
  conversation.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->
- **A cold turn cost $0.21 against ~20k cache-creation tokens**, measured on
  `g15` 2026-09-12 with claude 2.1.269. That is the number the warm base exists
  to remove, and it is also why a failed turn is retried cold only when the
  failure could actually be a dead `--resume` — see `meta/queue.py`.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->
- **The spec's *Liveness* section is deliberately not implemented.** It was
  written for a draft in which Claude polled the `message` table from outside
  the bot. "Claude is a handler, not a second process" was decided 2026-09-12
  and postdates it: inbound and outbound are one process now, so a dead bot
  means no turn is ever spawned rather than a turn answering into the void.
  Stated so it can be overruled in one line, not re-derived.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->
- **`extractable` is vestigial on purpose, and removing it is a contract step.**
  The verdict drives the extraction tail; `extractable` is still *written* in
  step with it and read by nothing. This was the expand half of an
  expand-contract migration, taken deliberately per the spec's
  migration-reversibility rule. Dropping the column is a later hand-taken step,
  not a tidy-up for whoever notices it is dead.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->
- **There is a bounded window in which chatter can coin facts, and it was
  chosen.** `record` commits the row as a `fact`, classifies, then writes the
  real verdict in a second transaction — so for one model call's width the row
  sits in the extraction tail, and a `/q` landing in that second can extract
  facts from it which the second transaction does not tombstone. The
  alternative was classifying before the commit, which loses the message
  outright when anything dies mid-handler, because aiogram advances the polling
  offset as it dispatches. Spurious removable facts beat lost messages. If it
  ever bites, the tombstone goes in the re-classification path.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->
- **The meta layer has never been walked on the dev bot.** As of 2026-09-12 the
  whole of it — hand-off, 👀, a real `claude -p`, the reply — has only ever run
  against fakes: the plan's live-walk steps were not executed, and the suite
  structurally cannot reach a subprocess. Three review rounds in that plan each
  found something the suite could not see, and a green 164-test run once sat on
  a module that could not be imported cold. Do the walk before trusting it.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->

## The repo's own automation, and the branch it shares

- **Orca's memory-harvest automation shares the `freeform-facts` working tree
  and rewrites history on it.** On 2026-09-12, while a plan was executing on
  that branch, `ff27aac` "harvest: 34 facts against 35574a2" was replaced by
  `db9de98` — same message, different tree, 402 insertions became 415. It
  touches docs and its own state only (`CLAUDE.md`, `docs/telegram-bot-api.md`,
  this file, `.claude/harvest/`, `.claude/kb-harvest-state.json`) and never
  code, but a rewrite underneath a running branch is still a rewrite: it cost
  that run two worktree moves and forced every review package to be scoped by
  explicit SHAs instead of `HEAD`, so an interleaved harvest commit could not
  appear inside a review diff. Branch long-lived work off your own last commit
  rather than off whatever `freeform-facts` points at, and merge back at the
  end.
  <!-- src: telegrind 5dcec58 | 2026-09-12 -->

<!-- KB refreshed against 5dcec58 on 2026-09-12 -->
