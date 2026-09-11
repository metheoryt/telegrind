# Telegrind bot

Your private telemetry through a Telegram chat. You write what happened; the
bot keeps it and answers questions about it. The dialogue is the product.

## How it works

Every message you send is stored, unchanged, the moment it arrives. Nothing is
parsed on receipt — asking is what triggers the parse. `/q сколько я потратил`
first reads the whole unparsed tail in one pass, so the model sees each message
in the company of its neighbours, and the categories it finds are the ones your
own messages turned out to need rather than a list declared up front. Then the
question itself becomes a database query: every number in an answer is counted
by Postgres, never by the model.

The bot does not echo. The one thing it says on ingest is a 💔 reaction, which
is both the receipt that the message landed and the button that takes it back:
tap the bubble and the facts derived from that message are tombstoned, tap it
again and they come back. The message itself is never deleted. Edit a message
and the heart changes — 💔 → ❤‍🔥 → 💘 → 💔 — so you can see the bot picked the
edit up.

## DONE

- Semantic date parsing (вчера, 2 hours ago, …), resolved against the message's
  own clock rather than the moment of parsing
- Every message stored unconditionally, with its extraction state on the row
- 💔 as the receipt and the delete affordance, cycling on every edit
- Batch extraction over a window, with the taxonomy observed from the chat
- `/q` — questions answered from the fact table, with the arithmetic done in SQL
- Edit a message and its facts are re-derived, in the company of its neighbours

## TODO

- Voice transcription
- Importing the existing chat history
- Kaspi PDF statement parsing
- Recurrent payments — manage and get notified when you need to pay
- Reports

## Running it

```bash
./dev-setup.sh        # writes .env and compose.override.yml, both gitignored
docker compose up
```

Or locally: `uv sync && python main.py`, with `alembic upgrade head` first.
Tests: `uv run pytest`. Lint: `uv run ruff check`. Types: `uv run ty check`.

## Deployment

Deployed on the homeserver by the `vps` repo poll-and-build pipeline — pushing to `main` is
the deploy. See `CLAUDE.md` and `vps/homeserver/DEPLOYING-A-REPO.md`.
