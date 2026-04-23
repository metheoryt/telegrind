# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Telegrind is an async Telegram bot that lets users track expenses, loans, and wishlists by sending natural-language messages. Records are written to a user-owned Google Sheets document. The bot uses aiogram, SQLAlchemy (asyncpg), and gspread-asyncio.

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

Copy `.env.dist` to `.env` and fill in `BOT_TOKEN`, `DATABASE_URL`, and `GOOGLE_SERVICE_ACCOUNT_FILE` before running.

There are no tests or linting scripts configured.

## Architecture

### Request Flow

```
Telegram message
  → Dispatcher (aiogram)
  → populate_chat_data middleware   # injects: session, chat, agc
  → handler (handlers.py)
  → Sheet subclass (sheets.py)      # parse + write to Google Sheets
  → reply to user
```

### Key Layers

**`telegrind/bot/middleware.py`** — `populate_chat_data` runs before every handler. It looks up (or creates) the `Chat` DB record for the current chat, authorizes the Google Sheets client (`agc`), and injects both plus the SQLAlchemy session into handler kwargs.

**`telegrind/bot/handlers/handlers.py`** — Four main handlers dispatched by aiogram filters:
- `record_outcome` — matches bare numbers/amounts (expenses)
- `record_loan` — matches `займ`/`долг` keywords (loans)
- `record_wish` — matches `хочу` keyword (wishlist)
- `update_changed_message` / `delete_record` — edit/delete via message edit or `-` reply

**`telegrind/sheets.py`** — Core logic. `Sheet` is the base class; `Outcome`, `Loan`, and `Wish` subclass it. Each subclass implements `make_row()` to convert a parsed Telegram message into a spreadsheet row, plus `record()`, `search_row()`, `change_row()`, `delete_row()`. `ConfigSheet` reads per-user timezone and currency from a `_config` worksheet.

**`telegrind/models.py`** — Two SQLAlchemy models: `Chat` (chat_id + sheet_url) and `File` (caches Telegram file_ids so the intro video isn't re-uploaded).

**`main.py`** — Creates the SQLAlchemy async engine, loads Google service account credentials, and starts aiogram polling. `async_session` and `AsyncioGspreadClientManager` are passed down through the dispatcher's workflow data.

### Onboarding Flow

`/start` triggers an FSM in `handlers/start.py`: the bot sends an intro video, asks for a Google Sheets URL, validates access, and stores the URL in the `Chat` record.
