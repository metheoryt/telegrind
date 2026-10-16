"""Deploy files say what the spec says (SPEC section 10)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_mounts_and_env():
    """REQ-DEPLOY-2, REQ-DEPLOY-3: the two mounts at the spec's paths, env from .env, unprivileged user."""
    compose = (ROOT / "compose.yml").read_text()
    for needle in ("./data:/data/telegrind", ":/claude", "env_file", "1000:1000"):
        assert needle in compose, needle
    dist = (ROOT / ".env.dist").read_text()
    names = (
        "BOT_TOKEN",
        "ALLOWED_USER_IDS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ELEVENLABS_API_KEY",
        "DATA_DIR",
        "ACCOUNT_HOME",
        "DEFAULT_TZ",
        "QUIET_HOUR",
        "BACKLOG_LIMIT",
        "TURN_QUOTA",
        "MAX_CONCURRENT",
    )
    for name in names:
        assert re.search(rf"^{name}=", dist, re.M), name


def test_dockerfile_installs_both_binaries_as_an_unprivileged_user():
    """REQ-DEPLOY-1, REQ-DEPLOY-4: claude and sqlite3 in the image; the process runs as `bot`."""
    text = (ROOT / "Dockerfile").read_text()
    for needle in ("sqlite3", "claude.ai/install.sh", "USER bot", 'CMD ["telegrind", "run"]'):
        assert needle in text, needle


def test_walkthrough_covers_the_spec_list():
    """REQ-TEST-5: the manual walk names every step the spec lists."""
    text = (ROOT / "docs" / "walkthrough.md").read_text().lower()
    for word in ("текст", "голос", "фото", "правк", "удал", "восстанов", "вопрос", "проход", "перезапус"):
        assert word in text, word
