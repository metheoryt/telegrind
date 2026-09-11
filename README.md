# Telegrind bot

Your private telemetry through a Telegram chat. You write what happened; the
bot keeps it and answers questions about it. The dialogue is the product.

## How it works

Every message you send is stored, unchanged, the moment it arrives. Nothing is
parsed on receipt — a later batch pass reads a window of messages at once, so
the model sees each one in the company of its neighbours, and the categories it
finds are the ones your own messages turned out to need rather than a list
declared up front.

The bot does not echo. The one thing it says on ingest is a 💔 reaction, which
is both the receipt that the message landed and the button that takes it back:
tap the bubble and the facts derived from that message are tombstoned, tap it
again and they come back. The message itself is never deleted.

## DONE

- Semantic date parsing (вчера, 2 hours ago, …), resolved against the message's
  own clock rather than the moment of parsing
- Every message stored unconditionally, with its extraction state on the row
- 💔 as the receipt and the delete affordance
- Edit a message and its facts are re-derived on the next pass

## TODO

- Batch extraction over a window, with the observed taxonomy
- `/q` — questions answered from the fact table
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
