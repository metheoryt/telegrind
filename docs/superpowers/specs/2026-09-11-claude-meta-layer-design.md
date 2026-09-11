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

The user sees one dialogue. Which runtime answered is visible from the shape
of the answer — a reaction is the bot, text is Claude — and never from a
mode, a prefix or a command.

## Architecture

```
message arrives
  → store verbatim, commit                 (unchanged, and unconditional)
  → classify: fact | question | talk       (one cheap call, after the commit)
      fact     → 💔, enters the extraction tail
      question → answer.spec_for → SQL → text        (the bot)
                 refused? → hand to Claude
      talk     → no reaction at all                  (Claude)
```

**The bot does not know Claude exists.** Claude reads the `message` table and
replies through the Bot API. There is no call from the bot into Claude, no
subprocess, no queue: the transport is the database that is already there.

**Claude does not run inside the bot.** The bot runs on the Anthropic API key
in a container; Claude runs as Claude Code on the subscription, outside it but
on the same machine. The two never share credentials, and the API key never
leaves the bot's runtime.

**Colocation is the design, not a deployment detail.** Claude sits on
`latitude` beside the database it reads and the container it rebuilds. That
one choice removes a published database port, a tailnet-bound tools service, a
registry round trip and a ten-minute deploy — see *The data tools* and
*Self-modification*.

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

### The receipt becomes the routing signal

💔 now means "understood as a fact, will extract it". No reaction means
"understood as a remark, replying in text". The user sees the routing
decision immediately, in the surface that already exists, and a misroute is
visible before the answer is.

This shifts the receipt's meaning from *parsed* to *will parse*: the
classifier can call something a fact that extraction later yields nothing
for. That is accepted. The alternative — placing the receipt after
extraction — would delay it by however long the user goes without asking a
question, which is the entire point of deferring extraction.

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

## The bot stores its own messages

Every message the bot or Claude sends is written to `message` with
`extractable=False`. `models.py` already anticipates this: the `extractable`
docstring names "imported bot replies" as a case.

It is load-bearing twice. A reply to something Claude said resolves to a
`reply_to_message.message_id` with a row behind it, so `extract._line` stops
emitting «ответ на сообщение вне окна». And Claude can see what it already
said, which is the whole of "access to the previous dialogue".

## Sessions

### A session is a reply chain, not stored state

Walk backwards from the incoming message through `raw.reply_to_message`. The
chain is the session. A plain message has no parent, so it starts a new one —
which falls out of the mechanism instead of needing a rule.

Nothing is stored, nothing expires, no timer job exists, and the session
cannot drift from what Telegram shows the user.

### TTL is a cutoff applied while walking

Stop walking when a hop is older than N minutes. A conversation resumed after
a long gap starts fresh even along a reply, which matches the intent: the user
is usually recording, not conversing, and a stale chain would carry irrelevant
context into a one-line exchange.

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
needs nothing extra, and a failed migration is a startup failure the health
check already catches.

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

"Came up" needs a definition before this is built, and the container merely
existing is not it: `restart: unless-stopped` means a crash-looping bot is
also a running container. The signal is the process staying alive for the
window without a restart, plus the log line that says polling started.

Claude is modifying the bot that delivers Claude its own inbound messages. A
broken deploy does not merely break the bot — it removes the only channel on
which the user could say "roll it back". The check costs nothing, asks the
user for nothing, and is the difference between a bad change and a dead
system.

## Liveness

The Bot API accepts `sendMessage` whether or not the bot process is running,
but Claude reads inbound from a table the bot process fills. A dead bot makes
Claude deaf while leaving it able to talk — it looks alive and is not.

Claude checks the age of the newest row before replying, and says so instead
of answering into the void.

## Non-goals

- **No second Telegram consumer.** Long polling is exclusive; a second reader
  of `getUpdates` would fight the bot for updates. The database is the
  transport, and that is not a preference.
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

## Open questions

- **Getting Claude Code onto latitude.** Node 20 is there; Claude Code is
  not, and the subscription login is interactive and has to be done by hand
  once. The user's wrappers arrive on their own — latitude carries the
  `agents` role, so `bootstrap.sh` deploys the plugin and skills there.
  Development still happens on `g15`, where everything is already installed
  and where the `tg.py` + database-polling shape was run by hand on
  2026-09-11.
- **The TTL value.** No measurement exists. Pick something, watch it.
- **What "the bot came up" means**, precisely enough to roll back on. See
  *The health check is not a gate*.

## Build order

1. The classifier, its verdict column, and `extractable` derived from it.
   Receipt only on facts. Nothing else changes.
2. The bot stores its own outbound messages.
3. Natural-language questions route to `answer.spec_for`; a refusal becomes a
   hand-off rather than a dead end.
4. The Claude runtime on dev: reads the table, replies through the Bot API,
   with the liveness check.
5. Sessions — the reply chain, the TTL cutoff.
6. The data tools.
7. Delivery: mount the sources in prod, then the restart script — two
   trees, tests, restart, health check, rollback. Only after this does
   self-modification mean anything.
8. Voice transcription on arrival.
