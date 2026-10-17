"""Deploy files say what the spec says (SPEC section 10)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_mounts_and_env():
    """REQ-DEPLOY-2, REQ-DEPLOY-3, REQ-LAYOUT-4: two mounts at fixed paths, env from .env, unprivileged."""
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


def _dockerfile_lines() -> list[str]:
    return [line.strip() for line in (ROOT / "Dockerfile").read_text().splitlines()]


def test_dockerfile_installs_both_binaries_as_an_unprivileged_user():
    """REQ-DEPLOY-1, REQ-DEPLOY-4: claude and sqlite3 in the image; the process runs as `bot`."""
    lines = _dockerfile_lines()
    assert any(line.startswith("RUN apt-get") and "install" in line and "sqlite3" in line for line in lines)
    assert any("claude.ai/install.sh" in line for line in lines)
    assert [line for line in lines if line.startswith("USER ")][-1] == "USER bot"
    assert 'CMD ["telegrind", "run"]' in lines


def test_compose_user_is_the_dockerfile_uid():
    """REQ-DEPLOY-4: compose runs as the uid the image's `bot` owns /app with."""
    uid = re.search(r"useradd\b.*-u (\d+)", (ROOT / "Dockerfile").read_text()).group(1)
    assert re.search(rf'^\s*user:\s*"{uid}:{uid}"\s*$', (ROOT / "compose.yml").read_text(), re.M)


def test_dockerfile_disables_the_cli_autoupdater():
    """The pinned claude holds only while the native auto-updater is off."""
    assert any(line.startswith("ENV") and "DISABLE_AUTOUPDATER=1" in line for line in _dockerfile_lines())


def test_deploy_notes_say_to_chown_the_mounts():
    """REQ-DEPLOY-4: Docker creates a missing bind source as root; the notes say to prepare it."""
    for name in ("CLAUDE.md", "docs/walkthrough.md"):
        assert "chown -R 1000:1000" in (ROOT / name).read_text(), name


def test_walkthrough_covers_the_spec_list():
    """REQ-TEST-5: the manual walk names every step the spec lists."""
    text = (ROOT / "docs" / "walkthrough.md").read_text().lower()
    for word in ("текст", "голос", "фото", "правк", "удал", "восстанов", "вопрос", "проход", "перезапус"):
        assert word in text, word


def test_dockerfile_pins_the_claude_the_live_gate_measured():
    """REQ-TEST-4, REQ-DEPLOY-1: the image runs the CLI version SPEC records the live gate as measured on."""
    pin = re.search(r"claude\.ai/install\.sh \| bash -s (\S+)", (ROOT / "Dockerfile").read_text()).group(1)
    measured = re.search(r"Claude Code\s+(\d+\.\d+\.\d+)", (ROOT / "SPEC.md").read_text()).group(1)
    assert pin == measured
