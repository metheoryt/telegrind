# The Claude meta layer — design

Status: approved in outline 2026-09-11, pending review of this document.
Extends `2026-09-11-dialogue-first-design.md`, which stays the design of the
mechanical bot. Nothing here changes what a fact is, how it is extracted or
how it is queried.

## Problem

The dialogue-first design left one surface undesigned: what happens to a
message that is not a fact.

Today every message is treated the same — stored, given a 💔 receipt, and
queued for extraction. That is wrong in three separate ways.

**The receipt lies.** 💔 means "tapping this deletes the facts on it". A
question has no facts, so the receipt promises a gesture that does nothing.
Walking Phase 2 on 2026-09-11 produced exactly this: a message marked
extracted with nothing on it, whose receipt deleted nothing.

**Chatter poisons the taxonomy.** The extraction tail carries every message
that is not a slash command. A question, a remark, a stray "ок" all reach
the extractor, which is obliged to coin a `kind` for each. The observed
taxonomy is what the next pass reads back, so one bad coinage is permanent
until someone notices.

**There is nobody to talk to.** The bot answers `/q` and is otherwise mute.
Saying *why* a fact was filed the way it was, or changing the rule that filed
it, has no channel at all. That is the half of the product the dialogue-first
design named and did not build.

## Goal

One Telegram chat, two runtimes behind it.

The **mechanical bot** records, reacts and counts. It is cheap, always on,
and never speaks except to answer a question about the data.

**Claude** talks. It is Claude Code on the subscription, with the user's own
skills and hooks, and it can change the mechanical bot.

The user sees one dialogue, and never a mode, a prefix or a command.

Which runtime answered is not always distinguishable, and deliberately so:
both send through the same token until the identities are split, and the bot
does send text — an answer to a question is text. What is distinguishable is
the *routing*, before any answer arrives: a 💔 means the message was taken as
a fact, 👀 means it went to Claude, and no reaction at all means it is queued
behind the turn in front of it. That is the signal the user needs, because it
is the one they can correct.

## Architecture

```
message arrives
  → store verbatim, commit                 (unchanged, and unconditional)
  → classify: fact | question | talk       (one cheap call, after the commit)
      fact     → 💔, enters the extraction tail
      question → answer.spec_for → SQL → text        (the bot)
                 refused? → hand to Claude
      talk     → queued for the session; 👀 when handed over   (Claude)
```

**Claude is a handler, not a second process.** Decided 2026-09-12, reversing
an earlier draft in which Claude polled the `message` table from outside. The
meta layer is an aiogram module the bot registers: it sees the update, it owns
the conversation, it spawns `claude -p` and it sends the reply. The reason is
reuse — the same module drops into any bot on `latitude`, and a poller that
reads one app's schema does not.

**It stays a guest in this bot.** Per-bot configuration is deferred, but the
coupling is not: the module takes an aiogram router, a config object and a set
of tools, and reaches into none of telegrind's internals. The conversation,
the sessions, the queue and the reactions are the reusable half; `query.py`
and the taxonomy are telegrind's, handed in as tools. Separating the two later
should be deleting a wiring file, not unpicking a merge.

What that costs, stated plainly rather than engineered around:

- **The two runtimes now share a container and therefore a credential store.**
  The subscription login sits beside the Anthropic API key. There is no
  boundary between them any more, and the admin allowlist is what is left.
- **The deploy half has to move out** — a handler cannot restart the process
  it is running in. See *The restarter lives outside*.

**Colocation is still the design, not a deployment detail.** Everything sits
on `latitude` beside the database. That one choice removes a published
database port, a tailnet-bound tools service, a registry round trip and a
ten-minute deploy — see *The data tools* and *Self-modification*.

## Who may talk to Claude

The bot has no sender gate today: `populate_chat_data` resolves — or creates —
a `Chat` row for whatever `chat_id` arrives, and nothing anywhere reads
`from_user`. That is harmless while the bot only records and counts. It stops
being harmless the moment text from a chat can commit, push and restart
production.

So the meta layer is gated on an **allowlist of admin chat ids**, configured
beside the token. A message from a chat outside it is still stored, still
classified and still reacted to — the recording half is unchanged — but it
never reaches Claude, and a refused question stays a refusal instead of
becoming a hand-off.

The gate is on the chat, not on the verdict: `talk` from a stranger is not
answered at all.

## Routing

### The classifier is one call, after the commit

The row is committed before anything else happens — the invariant from the
dialogue-first design is that nothing written is ever lost, and it must not
come to depend on a model call succeeding.

A classifier failure therefore defaults to **fact**: 💔 goes on, the message
enters the tail, and the behaviour is exactly what ships today. A hiccup
costs a routing decision, never a message.

### The verdict is a derived field, not a decision

It is stored on the `message` row and it is re-derivable, the same way
`extracted_at` is. Getting it wrong is recoverable in both directions:

- a question misread as a fact is stored, extracted, and its facts are
  tombstoned when someone notices;
- a fact misread as a question is stored and simply unextracted, and flipping
  the verdict puts it back in the tail.

This is what makes automatic routing safe enough to have no confirmation step.
Nothing is lost either way, because storage is unconditional and extraction is
already re-runnable.

### It replaces `extractable`, it does not join it

`extractable` is set mechanically today ("starts with a slash"). It becomes
derived from the verdict: only `fact` enters the tail. Two flags that can
disagree is a bug found in production, not a design.

**The verdict is therefore never null**, which means rows the classifier never
sees need a value of their own. Any other slash command, and every message the
bot or Claude sends, is `system`. Without it, null would mean three different
things at once — not classified, classifier failed, classifier said not a fact
— and the flag would be undebuggable exactly when it misroutes. A classifier
failure writes `fact` explicitly, per the default above; `/q` writes
`question`, because the override sets the verdict rather than bypassing it.

### The receipt becomes the routing signal

There are three states, and one emoji each:

- **💔 — understood as a fact, will extract it.** Tapping it still deletes,
  which is what makes the whole `RECEIPT_CYCLE` of hearts mean one thing.
- **👀 — handed to Claude.** A different emoji rather than a second heart,
  precisely because it does *not* delete: the gesture and the family of emoji
  that carries it stay matched.
- **nothing yet — queued.** The message is classified and stored, and the
  session in front of it has not finished. The absence is not a state the
  design ran out of names for; it is the queue, visible.

The user sees the routing decision immediately, in the surface that already
exists, and a misroute is visible before the answer is. 👀 also answers the
question the user actually has while waiting — *did it get this one?* — which
no amount of typing indicator does.

The bot never receives `message_reaction` for reactions it set itself, so
placing 👀 cannot feed back into the delete path. A user tapping 👀 is an
ordinary user reaction and tombstones the message's facts — of which a talk
message has none, so it is a no-op.

This shifts the receipt's meaning from *parsed* to *will parse*: the
classifier can call something a fact that extraction later yields nothing
for. That is accepted. The alternative — placing the receipt after
extraction — would delay it by however long the user goes without asking a
question, which is the entire point of deferring extraction.

### An edit is a new message, and it is re-classified

`upsert_message` already overwrites the row and clears `extracted_at`, so an
edited message returns to the tail. The verdict is re-derived on the same
principle, because an edit can change the answer: correcting «взял 3000» to
«потратил 3000 на такси» is precisely the case where a fact moves from one
`kind` to another, and correcting data or a typo is what edits are actually
used for.

The verdict can also flip. `fact → talk` clears the receipt
(`setMessageReaction` with an empty list); `talk → fact` places it. Either way
the reaction after the edit still describes the current routing, which is the
only property the receipt has to keep.

`RECEIPT_CYCLE` keeps working on the fact path. On the talk path 👀 is
already there and stays; the re-answer is the acknowledgement, and it is a
louder signal than advancing an emoji.

**👀 is the point of no return: an edit after it is ignored entirely.** No
re-send, no new turn, and no re-classification either — the row is still
overwritten, because nothing written is ever lost, but nothing downstream
reacts to it.

The correct behaviour would be to rewind the session: drop that turn and
everything the model said after it, then replay. That is what editing a
message in a conversation actually means. It is also a transcript surgery on
a `.jsonl` we do not own, for a gesture that has a one-message workaround —
the user says the correction, and the session sees it, which is how correcting
a person works too.

Before 👀 there is no special case at all. The message is still in the queue,
`upsert_message` overwrites it, and what gets handed over is the edited text.
The reaction is the line, and it is the line the user can see.

### `/q` survives as an override, not as the way

The classifier takes questions, so asking no longer requires a command. `/q`
stays because it costs nothing and is useful twice: when the user wants to be
sure they are asking, and when the classifier got it wrong.

### A refused question falls through to Claude

`answer.spec_for` already refuses a question that does not fit the closed set
of aggregates, rather than approximating it. Today that refusal is a dead end.
It becomes a hand-off: the bot could not answer it, so Claude does.

**This is what lets the classifier's boundary be soft.** «сколько потратил в
сентябре» is data and «почему ты записал это расходом» is conversation, and
the line between them is genuinely blurry. With the fall-through, a misroute
costs a second of latency instead of an unanswered question.

### Cost

One model call per incoming message, before the reaction appears. The receipt
stops being instant and becomes roughly a second. Extraction stays deferred
and windowed — this call classifies, it does not extract, and it reads one
message rather than a window.

### Where Claude attaches: a handler now, a middleware later

At steps 1–4 Claude only ever receives what the bot already refused, so it
attaches at the end of ingest: `route()` decides, and a hand-off is one call
into the queue. Not making that call is what "Claude off" means — no handler
edits, no flag threaded through the routing.

An aiogram middleware instead of a handler is the wrong shape at this stage
and the right one later, and the line between the two is what Claude is
allowed to see.

- **An inner middleware (`router.message.middleware`) buys nothing here.** It
  wraps the handler, so it runs inside the session `populate_chat_data`
  opened. To learn what the handler stored it has to read on that session, and
  a bare read there autobegins — the next `session.begin()` then raises «a
  transaction is already begun». That trap is documented at `query.py` and has
  already been hit in an edit path and in the meta layer's own `speak`. The
  handler seam keeps routing a plain function over a row, testable without
  assembling an aiogram update.
- **An outer middleware (`dp.update.outer_middleware`) is the seam for a
  Claude that sees everything.** It runs before filters and before handler
  resolution, on every update — including the reactions and commands the bot
  answers itself — and it may decline to call the handler at all. That is the
  mechanism a *preempting* meta layer needs, and it is worth its cost only
  once Claude has something to say about an update the bot already handles.
  That is steps 5–8, not step 4.

Until then the rule is that Claude sees a message because routing handed it
one, never because it was listening in.

## The bot stores its own messages

Every message the bot or Claude sends is written to `message` with
`extractable=False`. `models.py` already anticipates this: the `extractable`
docstring names "imported bot replies" as a case.

It is load-bearing twice. A reply to something Claude said resolves to a
`reply_to_message.message_id` with a row behind it, so `extract._line` stops
emitting «ответ на сообщение вне окна». And Claude can see what it already
said, which is the whole of "access to the previous dialogue".

## Sessions

### A session is a reply chain, and it is never walked

Read `raw.reply_to_message` — one hop, not a walk. Its `message_id` gives the
parent's session id, the turn forks from it, and the history comes along
inside the fork. The chain lives in the transcripts; the database only ever
answers "what is the parent".

A plain message has no parent, so it forks the warm base instead and starts a
new conversation — which falls out of the mechanism instead of needing a rule.

Nothing is stored, nothing expires, no timer job exists, and the session
cannot drift from what Telegram shows the user.

**Claude always sends with `reply_to_message_id`.** Without it a reply to
Claude has no parent row to point at, and every second turn would start from
the warm base instead of from the conversation. It is one argument on
`sendMessage`, and it is what keeps the tree in the transcripts identical to
the tree Telegram draws on screen.

### There is no TTL

An earlier draft cut the walk at a hop older than N minutes. It is not needed:
the chain *is* the session, and starting a fresh one is already a gesture the
user makes without thinking — sending a message instead of replying to one. A
reply to something from last week is a deliberate resumption, and honouring it
costs nothing, because the runtime keeps the session on disk (see *The Claude
runtime*).

### Reply means two different things, and they separate mechanically

- **Reply to one's own message** — fact chaining. Two messages become one fact
  only when mechanically joined; this is that join, and it is the rule settled
  on 2026-09-11 after adjacent facts collapsed onto the wrong message.
- **Reply to a Claude message** — conversation. It continues the session.

Because a pure record gets no text reply, there is nothing of Claude's to
reply to on the recording path, and the two uses cannot collide.

### The history is a query, not a context window

"Access to the whole previous dialogue" is not a context-window problem.
Claude has tools over the `message` table; it reads what it needs when it
needs it. The session governs conversational continuity only.

## Voice and photos

**Voice is transcribed on arrival** and the transcript is treated exactly as
text — the `transcript` and `transcript_model` columns already exist. A voice
`/q` starts working as a side effect; it was unsupported for want of ASR, not
as a policy.

**Photos are stored, the caption is the content, and nothing looks at the
image.** Vision on ingest is out of scope here.

## The data tools

### Why not raw SQL

`query.py` carries guards that are easy to forget and invisible when
forgotten: `deleted_at IS NULL` on every read, and
`jsonb_typeof(fields->'x') = 'number'` before every cast. Claude writing SQL
by hand will drift off them, and then Claude and `/q` report different numbers
for the same question — silently, because both look right.

So Claude reaches the data through **the same closed surface `/q` uses**. The
tools are a second caller of `query.py`, not a second implementation.

### The surface

Read-only to begin with:

- `messages` — a window, or a search, over the dialogue
- `aggregate` — the closed set from `query.py` (`sum`, `count`, `avg`, `min`,
  `max`, `last`, `balance_by`)
- `taxonomy` — the kinds and field names this chat has actually coined

Writes (`re-extract this message`, `tombstone this fact`) are deliberately a
later step. Every one of them already has a user-facing gesture, and a tool
that duplicates a gesture is a second way for the two to disagree.

### Where it runs

**Claude runs on `latitude`, beside the database and the container it
manages.** That removes the network question entirely: the tools talk to
postgres over the compose network, no port is published, no tunnel exists,
and nothing binds to the tailnet. A plain library import is enough, the same
as on dev — MCP is what colocation buys us out of, not what it requires.

Dev is local and needs none of this: `localhost:5433` already works.

## Self-modification

### Automatic, with no per-change gate

Decided 2026-09-11. Claude commits and pushes; the change reaches production
without a confirmation step. A gate can be added later if it turns out to be
needed, and adding one is cheap; the point is not to build the ceremony before
knowing whether it earns its keep.

### The restarter lives outside

A handler cannot restart the process it runs in: `docker compose restart bot`
issued from inside the turn kills the turn. And reaching the docker socket
from inside the container means mounting it, which is root on `latitude` —
handed to a process whose input is chat text and whose permission mode is
`bypassPermissions`. The admin allowlist would be the only thing left between
a message and the host.

So the handler does not deploy. It **requests** a deploy — a row, with the SHA
it wants — and a small runner outside the container does the rest: fetch,
reset, migrate, restart, watch, roll back. It is a few dozen lines, it holds
the only docker access, and it is the one thing here that is not written by
the model it serves.

This also keeps the module reusable, which was the point of making it a
handler at all. Deploying is per-app; conversation is not.

### Claude never edits the live tree

`vps/homeserver/telegrind/src/` is the deployed checkout, and `restart:
unless-stopped` is on the prod bot. Mounting that tree into the container
means the running code *is* the tree — so if it is also the tree being
edited, a crash at the wrong moment reloads half-written code and the
container crash-loops on it.

So there are two trees on latitude. Claude works and commits in its own
checkout; the deployed tree is updated by one command (`git fetch` then
`reset --hard`) immediately before the restart. **The restart is the only
moment the running code changes**, which is what makes the change atomic
enough to roll back.

### Delivery is a local restart, and usually not even a rebuild

**Mount the sources into the container.** The dev stack already does exactly
this — `.:/app` plus an anonymous `/app/.venv` so the image's virtualenv is
not shadowed by the host's — and prod differs only in not doing it. With the
mount, the image stops being the artifact of a commit and becomes the
environment: python plus the locked dependencies. The code is the mount.

Then a deploy is `docker compose restart bot`, which is seconds. `entrypoint.sh`
runs `alembic upgrade head` on every start, so a change that needs a migration
needs nothing extra on the way *out*, and a failed migration is a startup
failure the health check already catches. The way back is not symmetric — see
below.

**A rebuild is needed only when the environment changed** — `uv.lock`,
`pyproject.toml` or the `Dockerfile`. Comparing those against the previous
deploy is the whole test.

### Why not the registry path

`embedthat` moved to a tag-triggered GitHub Actions build on 2026-09-08, and
that was the obvious route to copy — until Claude moved onto the same box as
the bot. Once it is there, the round trip through GitHub and Tugtainer buys
nothing and costs the two things that matter here: over ten minutes of
latency, and a rollback that has to go back out to the network to happen.

What the registry route gives up in return is an immutable artifact per
release. Rollback becomes a SHA rather than a digest, and a dependency
rollback forces a rebuild instead of a pull. Both are accepted.

**The standing ban on tagging `metheoryt/telegrind-bot:*` therefore stays.**
It exists because a registry tag would let Tugtainer pull-update over a
locally built container and silently undo a deploy; the local build is not
going away, so neither is the hazard.

Claude still pushes to GitHub — that is where the history and the review live —
but the deploy does not wait on it, and a GitHub outage does not stop a
rollback.

### Tests run before the restart, not in CI

There is no CI on this path, and there was none on the registry path either:
`embedthat`'s workflow builds and publishes, it does not test. Running the
suite locally before restarting is strictly more than either, and it is the
natural place for it — the same process that is about to deploy is the one
that can decline to.

### The health check is not a gate

Record the SHA before the restart. Restart. Watch for N seconds; if the bot
did not come up, `reset --hard` back to the recorded SHA and restart again.
The whole loop is local and takes seconds — no network, no registry, no
GitHub.

**Rolling back the code does not roll back the schema.** `alembic upgrade head`
runs on every start, so by the time the health check fails the migration has
already applied, and `reset --hard` alone leaves old code facing a new schema —
which is a second outage, not a recovery. So the rollback is
`alembic downgrade <the recorded SHA's revision>` first, then the reset, then
the restart.

**That makes reversibility a rule for writing migrations, not a step in the
script.** Every migration carries a real `downgrade`, and a change that cannot
be reversed — a dropped column, a destructive backfill — is not written as one
migration. It is split: an expand step that deploys and is reversible, and a
contract step taken later, by hand, once the new code has held. Storage is
unconditional here; losing a column on a rollback would break that.

**"Came up" means polling started.** The container merely existing is not it:
`restart: unless-stopped` means a crash-looping bot is also a running
container. The signal is the log line that says long polling began, and the
process still holding it at the end of the window.

Claude is modifying the bot that delivers Claude its own inbound messages. A
broken deploy does not merely break the bot — it removes the only channel on
which the user could say "roll it back". The check costs nothing, asks the
user for nothing, and is the difference between a bad change and a dead
system.

## How Claude speaks

**Lazily, and in the register of a chat.** A detailed explanation is still a
conversation, not an essay: nobody answers with a wall of text in Telegram,
and nobody reads one on a phone. Claude says the short thing and expands when
asked — which is also the cheapest thing to do with a model that is charged
by the token.

**The waiting is shown with a draft, not a typing indicator.** There is no
chat-action machinery left to reuse — `setup.py` records that the
`ChatActionMiddleware` "went with the echo". `sendMessageDraft` is the
better surface anyway and is callable at the pinned aiogram 3.27 (Bot API
9.5): private chats only, and re-sending the same non-zero `draft_id`
animates the change client-side. That is a streaming answer, not a fake
ellipsis.

**4096 characters is a hard limit, and the answer is not to split.** Splitting
a long reply into three bubbles is the wall of text with extra steps. When
something genuinely does not fit a conversation — a report, a table, a diff —
it leaves the chat as an object: a file, or a page. Rich Messages (Bot API
10.1 — tables, expandable quotation, a thinking block) is the native version
of this and needs the bump to aiogram 3.31; until then, a document.

## Liveness

The Bot API accepts `sendMessage` whether or not the bot process is running,
but Claude reads inbound from a table the bot process fills. A dead bot makes
Claude deaf while leaving it able to talk — it looks alive and is not.

Claude checks the age of the newest row before replying, and says so instead
of answering into the void.

## When a turn fails

👀 is a promise, and a crashed or hung `claude -p` breaks it silently — which
is the same failure the liveness check exists for, one layer in.

So the turn has a timeout, its exit code is read, and either failure is
visible in the chat: the 👀 comes off and Claude says what happened. A
silence the user cannot tell from thinking is worse than an error message.

## Non-goals

- **No second Telegram consumer.** Long polling is exclusive; a second reader
  of `getUpdates` would fight the bot for updates. That is why the meta layer
  is a handler inside the one process that polls, and not a preference.
- **No mode switch.** There is no command to talk to Claude and none to stop.
  The classifier routes; the reaction shows what it decided.
- **No echo.** A recorded fact still produces no text. "Записал" is the
  register for when Claude does speak, not a per-message reply — 💔 already
  says it, silently.
- **No vision, no inventory-by-photo.** Named in the dialogue-first spec's
  *Later, not now*, and staying there.

## A second bot identity — later, and the seam to leave

The meta layer could be its own Telegram bot rather than telegrind's own
token: a group of {user, telegrind, claude}, with `@claude` as the address.
Telegram now supports this — Bot API 9.0 added bot-to-bot messaging by
username when both bots opt in, and 10.0 added seeing certain messages sent
by other bots in groups, plus guest mode for replying in chats a bot has not
joined. 9.0 is callable at the pinned aiogram 3.27 (Bot API 9.6); 10.0 needs
the bump to 3.31.

**None of it is needed here.** The two runtimes coordinate through a database
on one machine, so there is nothing for them to say to each other over
Telegram. Bot-to-bot solves a problem colocation already removed.

What a split would cost is a move from a private chat to a group, and two
specific things break on the way:

- **Privacy mode.** A bot in a group receives only mentions, commands and
  replies to itself unless privacy mode is turned off in BotFather.
  "Write anything and it is recorded" does not survive with it on.
- **`message_reaction` requires the bot to be an administrator in the chat.**
  In a private chat the bot is privileged by construction, which is why the
  receipt-and-delete gesture works today. In a group it stops arriving until
  telegrind is made an admin, and the whole delete affordance rides on it.
  (Related and already true: the update is not delivered for reactions set by
  bots, so the bot never sees its own receipt.)

And `@claude` is precisely the mode switch this design lists as a non-goal.
It can live as an explicit override, the way `/q` does — but not as the way.

**What is actually reusable is not the bot identity.** It is the runtime and
the deploy loop: a Claude Code process colocated with an app, reading that
app's database, replying through that app's token, and rebuilding that app on
a restart-and-roll-back cycle. `embedthat` runs on the same `latitude`, so one
Claude runtime there can serve both today, with per-app configuration — which
database, which token, which chat.

**The seam to leave open is one line: the token Claude sends through is
configuration, not a constant.** Splitting later then means a different token
and a group chat, and nothing in this design changes.

## The Claude runtime

**One `claude -p` per turn, forked from the session it is answering.** This is
full Claude Code, not a trimmed one: the same CLAUDE.md, skills, hooks and MCP
servers the user has in the TUI. Measured on `g15`, 2026-09-12:

- `claude -p --session-id <uuid> "..."` starts a session under an id we choose;
  `claude -p --resume <uuid> "..."` continues it and does remember the first
  turn. `--output-format json` returns the `session_id` and the cost; the
  transcript is one `.jsonl` per session under `~/.claude/projects/<cwd-slug>/`.
- `--resume <base> --fork-session --session-id <new>` forks: it keeps the
  history and writes it under the id *we* name. Verified — the fork answered
  from the base's first turn.

**A warm base session, forked per turn.** A cold session pays ~20k tokens of
cache-creation for the preamble; a fork off a warm base paid 2.9k against 32k
of cache read. So one empty session is created per day with everything loaded,
and every conversation forks from it.

**The fork point is the turn being replied to.** A turn is named by the user
message that caused it, so its session id is `uuid5(chat_id, that message_id)`
and it holds both the message and Claude's answer to it. Resolving the parent
is therefore at most two hops:

- the parent is the user's own message → that is the turn;
- the parent is a Claude message → the turn is the message *it* replied to,
  which exists because Claude always sends with `reply_to_message_id`.

Both land on the same session, which is the point: replying to one's own
question and replying to the answer are the same place in the conversation,
and a user should not have to know which one continues it.

Nothing is stored to make this work. The id is derived, so the session graph
cannot drift from the chat.

Flags: `--permission-mode bypassPermissions` and `--permission-prompts none` —
without the second, anything that would have prompted is silently denied
instead.

### One turn at a time per session, and the rest queue

**The queue is keyed by the session id**, and that one rule covers every case.
Same key, wait; different key, run now.

A follow-up is a reply to one's own message, which is the natural gesture and
resolves to the same key. What happens then depends only on whether the turn
has started:

- **Not handed over yet** (no 👀): the two go in together as one prompt. A
  correction sent two seconds later belongs to the same thought, and one turn
  seeing both answers better than two turns each seeing half.
- **Already running:** the follow-up waits, and then runs as the next turn on
  that session. This is not the consolation prize — by then the answer to the
  first message exists, and the follow-up is answered in light of it, which
  merging could never have done.

Waiting is visible without a word being sent: the running message carries 👀
and the queued one is still bare.

**Two replies to genuinely different points run concurrently.** Different fork
points are different sessions; serialising them would merge two threads the
user deliberately split. Fork-per-turn is what makes that safe — every turn
writes to its own id, so nothing interleaves in one transcript.

👀 goes on when the message is handed to the process, not when it is queued —
so the queue is legible on screen without a word being sent: the messages
still bare are the ones not yet seen.

## Open questions

- **Making the rebuild invisible.** Rebuilding the warm base is an explicit
  command for now, run when the user knows something changed — a new skill, an
  edited CLAUDE.md. Inferring it instead is a later problem, and guessing
  wrong costs a session that silently lacks the tool it needs.
- **How the module is configured per bot** — which token, which chat
  allowlist, which working directory, which tools. Deferred on purpose; see
  the coupling rule in *Claude is a handler*.

## Build order

1. The classifier, its verdict column (including `system`), `extractable`
   derived from it, and re-classification on edit. Receipt only on facts.
2. The bot stores its own outbound messages.
3. Natural-language questions route to `answer.spec_for`; a refusal becomes a
   hand-off rather than a dead end.
4. The Claude handler on dev: the admin allowlist, `claude -p` under a derived
   session id, the queue and its 👀, the turn timeout, replies sent with
   `reply_to_message_id`, the liveness check.
5. Sessions — the warm base, and forking per turn from the parent message.
6. The data tools.
7. Delivery: mount the sources in prod, then the outside runner — two trees,
   tests, migrate, restart, health check, rollback with `downgrade`. Only
   after this does self-modification mean anything.
8. Voice transcription on arrival.
