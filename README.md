# telegrind

telegrind is a multi-user Telegram log: Python stores every message in SQLite as it arrives, and a headless Claude Code agent, run through the `televibe` library, derives the facts and answers questions. `SPEC.md` is the law; when the code and the spec disagree, the spec wins or is amended first. To run it: `uv sync -p /usr/bin/python3.12 && uv run pytest`, then copy `.env.dist` to `.env`, fill it in, run `mkdir -p ./data ~/.claude-telegrind && chown -R 1000:1000 ./data ~/.claude-telegrind`, and run `docker compose up -d --build`. `docker compose exec bot telegrind doctor` checks the image.

See [SPEC.md](SPEC.md) and [docs/walkthrough.md](docs/walkthrough.md).
