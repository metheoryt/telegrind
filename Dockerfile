FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates sqlite3 git \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 1000 bot && useradd -m -u 1000 -g 1000 bot

# The claude CLI, pinned, installed for the bot user (it lives under its home).
# The pin holds only with DISABLE_AUTOUPDATER=1, set below: the bot user owns the install and would self-update.
USER bot
RUN curl -fsSL https://claude.ai/install.sh | bash -s 2.1.301
ENV PATH=/home/bot/.local/bin:$PATH DISABLE_AUTOUPDATER=1

USER root
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev
COPY telegrind ./telegrind
COPY agent ./agent
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev && chown -R bot:bot /app

USER bot
ENV PATH="/app/.venv/bin:$PATH" DATA_DIR=/data/telegrind ACCOUNT_HOME=/claude
CMD ["telegrind", "run"]
