# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Telegrind is an async Telegram bot that keeps a personal log. You write what
happened in natural language; every message is stored verbatim in Postgres on
arrival, and a later batch pass derives facts from it. The dialogue is the
product — there is no spreadsheet. The bot uses aiogram, SQLAlchemy (asyncpg)
and the Anthropic API.

The design this is being built to is `docs/superpowers/specs/2026-09-11-dialogue-first-design.md`;
Phase 1 (store, react, tombstone) is done, Phase 2 (batch extraction and `/q`)
and Phase 3 (history import) are not.

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

Production runs on the homeserver (`g513ie`) through the **config-driven poll-and-build**
pipeline owned by the `vps` repo — no registry, no CI publish. **Pushing to `main` is the
deploy**; it lands within ~3 minutes plus build time.

- The `repos-deploy` scheduled task runs `vps/homeserver/deploy-repos.ps1` every 3 minutes
  over every entry in `vps/homeserver/repos.psd1`.
- Per repo it fast-forwards a gitignored clone at `vps/homeserver/telegrind/src/` and, only
  when the source SHA or the rendered compose config changed, archives the container logs,
  rebuilds, and recreates the stack.
- The prod compose lives in `vps` (`vps/homeserver/telegrind/compose.prod.yml`), not here —
  this repo carries only the dev `compose.yml`. Prod secrets (`.env.prod`) live in
  `vps/homeserver/telegrind/`, outside the `./src` build context.
- The prod stack is project `telegrind` with the named volume `telegrind_pgdata`; the dev
  stack is project `telegrind-dev`. Never `docker compose down -v` on prod — it orphans the
  database volume.

**Never tag an image `metheoryt/telegrind-bot:*`.** The prod image tag is local-only
(`telegrind-bot:local`) on purpose: a registry tag would let Tugtainer pull-update the
container out from under the local build and silently undo a deploy.

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

There is no reply. The only outward signal on ingest is `setMessageReaction`
with 💔, and tapping that same bubble is how the user deletes:

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
`acknowledge` places the 💔 and never raises: the row is already committed, so
a Telegram failure costs a visual cue and nothing else.

**`telegrind/bot/handlers/reactions.py`** — *any* user reaction tombstones the
message's facts; removing it restores them. Registering the observer is what
subscribes the `message_reaction` update type.

**`telegrind/store.py`** — the message and fact repository. `upsert_message`
appends on first sight and overwrites on edit, clearing `extracted_at` so the
next batch pass picks the message up again. A forwarded message is dated by its
origin, not by the forward.

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

**`telegrind/llm.py`** — Phase 1 makes no LLM call. What survives is the client,
the model names, and `EXTRACTION_RULES`: accumulated judgement about real
messages, which Phase 2 composes with the observed taxonomy.

