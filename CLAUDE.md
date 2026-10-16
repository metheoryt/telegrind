# CLAUDE.md

Guidance for agents working on telegrind v3.

telegrind is a multi-user Telegram log: Python stores every message in SQLite the moment it arrives, and a headless Claude Code agent (run through the `televibe` library) derives facts from the log and answers questions. `SPEC.md` is the law: when code and spec disagree, the spec wins or is amended first. Requirements are cited as `REQ-*` in docstrings and tests.

## Layout of `telegrind/`

- `config.py` — configuration from the environment and nothing else.
- `layout.py` — one data directory, one sub-directory per person.
- `incoming.py` — what a Telegram message is, lifted into the log's columns.
- `log.py` — the log (`log.sqlite`): what Telegram sent, nothing derived.
- `receipts.py` — the heart reaction: «stored; tap to delete».
- `triggers.py` — what is a question; no model decides this for text.
- `facts.py` — read-only reads of the agent's `facts.sqlite`.
- `app.py` — the message path: store first, then react, enqueue or ask.
- `agent.py` — agent turns (a pass over the tail, or an answer) through televibe.
- `media.py` — photos and documents into `media/<sha256>`, after the commit.
- `voice.py`, `scribe.py`, `cleanup.py`, `keyterms.py` — voice notes: ElevenLabs Scribe, cleanup, transcript post.
- `scheduler.py` — when a pass runs: the quiet hour, or a backlog over the limit.
- `bot.py` — aiogram wiring: three handlers, each a line.
- `cli.py` — `telegrind run` and `telegrind doctor`.

`agent/` is the agent's working tree (its prompt and settings), copied into the image.

## Running

```bash
uv sync -p /usr/bin/python3.12        # a non-freethreaded CPython 3.12+ (python3.14 where that is what the box has)
uv run pytest                         # no network, no live agent
uvx ruff check telegrind tests
uvx ty check
TELEGRIND_LIVE_HOME=~/.claude-telegrind uv run pytest -m live   # live gate: real claude, costs tokens
```

**Never run a bare `uv sync`.** uv then picks its own freethreaded CPython 3.14, a dependency has no wheel for it, and the sync fails and takes the venv with it.

## Traps

- **Commit before any network or model call.** aiogram advances the polling offset as it dispatches; an update lost inside a handler is never redelivered. Store, then react, then call out.
- **`edit_date` arrives as a raw Unix int**, not a datetime; an edit path that assumes otherwise dies on every edit.
- **A reaction failure is swallowed**, so a wrong emoji looks like nothing happening. Probe each emoji live.
- **The turn's final text is what gets stored**, not `sent.text`: televibe's `Presenter` sends the answer as a rich message.
- **Record fixtures with `--setting-sources project`.** SessionStart hooks on a dev box inject personal memory into a headless `claude -p` call.
- **`log.sqlite` is used from the event-loop thread only.** No method is thread-safe.
- **A pass's «before» mark is read on the `Started` event**, not at queue time.
- **The manual walkthrough is not ceremony** (`docs/walkthrough.md`). Every end-to-end walk of this family of bots has found a defect the suite structurally could not see.
- **Never `source .env`** (CRLF and secrets leak into failure output) and **never dump `docker compose config`**: it prints every resolved secret. `BOT_TOKEN` and `ELEVENLABS_API_KEY` are stripped from every turn's env by the code; keep it that way. `.env.dist` carries placeholders only.
- **Sessions are keyed by cwd and config dir**, so the paths `/data/telegrind` and `/claude` never move.

## Deploy

Production is `latitude`, docker compose project `telegrind3`. Nothing auto-deploys: `git pull` there, then `docker compose up -d --build`. Before the first `docker compose up`, run `mkdir -p ./data ~/.claude-telegrind && chown -R 1000:1000 ./data ~/.claude-telegrind` (`sudo chown` if needed): Docker creates a missing bind source as root, and the uid-1000 bot then cannot write it; `telegrind doctor` shows "NOT OURS" when this was skipped. State lives in `./data` (mounted at `/data/telegrind`) and `~/.claude-telegrind` (mounted at `/claude`); never `docker compose down -v`. The image installs `claude` and `sqlite3`; check a running container with `docker compose exec bot telegrind doctor` (exit 1 means something is missing).
