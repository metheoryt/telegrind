"""Configuration is environment only (REQ-DEPLOY-3); ALLOWED_USER_IDS is required (REQ-ACCESS-1)."""

from pathlib import Path

import pytest

from telegrind.config import Config, ConfigError

BASE = {"BOT_TOKEN": "123:abc", "ALLOWED_USER_IDS": "7, 42"}


def test_defaults():
    """REQ-DEPLOY-3, REQ-TURN-10: the spec's defaults (a pass 600 s, a question 300 s); absent tokens are None."""
    cfg = Config.from_env(BASE)
    assert cfg.bot_token == "123:abc" and cfg.allowed_user_ids == frozenset({7, 42})
    assert cfg.claude_token is None and cfg.elevenlabs_key is None
    assert cfg.data_dir == Path("/data/telegrind") and cfg.account_home == Path("/claude")
    assert (cfg.default_tz, cfg.quiet_hour, cfg.backlog_limit, cfg.turn_quota, cfg.max_concurrent) == (
        "Asia/Almaty",
        4,
        50,
        50,
        2,
    )
    assert (cfg.pass_timeout_s, cfg.question_timeout_s) == (1800.0, 1800.0)


def test_every_variable_is_read():
    """REQ-DEPLOY-3, REQ-TURN-10: the full list, as the spec names it; both turn timeouts are configurable."""
    cfg = Config.from_env(
        {
            **BASE,
            "CLAUDE_CODE_OAUTH_TOKEN": "t",
            "ELEVENLABS_API_KEY": "e",
            "DATA_DIR": "/d",
            "ACCOUNT_HOME": "/a",
            "DEFAULT_TZ": "Asia/Bangkok",
            "QUIET_HOUR": "3",
            "BACKLOG_LIMIT": "10",
            "TURN_QUOTA": "5",
            "MAX_CONCURRENT": "1",
            "PASS_TIMEOUT_S": "60",
            "QUESTION_TIMEOUT_S": "30",
        }
    )
    assert cfg.claude_token == "t" and cfg.elevenlabs_key == "e"
    assert cfg.data_dir == Path("/d") and cfg.account_home == Path("/a") and cfg.default_tz == "Asia/Bangkok"
    assert (cfg.quiet_hour, cfg.backlog_limit, cfg.turn_quota, cfg.max_concurrent) == (3, 10, 5, 1)
    assert (cfg.pass_timeout_s, cfg.question_timeout_s) == (60.0, 30.0)


def test_blank_tokens_are_none():
    """REQ-TURN-1, REQ-VOICE-2: an empty variable means the feature is off, not an empty secret."""
    cfg = Config.from_env({**BASE, "CLAUDE_CODE_OAUTH_TOKEN": "  ", "ELEVENLABS_API_KEY": ""})
    assert cfg.claude_token is None and cfg.elevenlabs_key is None


@pytest.mark.parametrize(
    "env",
    [
        {"BOT_TOKEN": "x"},
        {"BOT_TOKEN": "x", "ALLOWED_USER_IDS": ""},
        {"BOT_TOKEN": "x", "ALLOWED_USER_IDS": "7,abc"},
        {"ALLOWED_USER_IDS": "7"},
        {**BASE, "QUIET_HOUR": "24"},
        {**BASE, "MAX_CONCURRENT": "0"},
        {**BASE, "DEFAULT_TZ": "Mars/Olympus"},
    ],
)
def test_refuses_to_start_on_bad_config(env):
    """REQ-ACCESS-1: no allowlist, no start; a wrong value is a ConfigError naming the variable."""
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_repr_hides_secrets():
    """REQ-DEPLOY-3: no secret is ever logged — repr() carries none."""
    text = repr(Config.from_env({**BASE, "CLAUDE_CODE_OAUTH_TOKEN": "sekrit", "ELEVENLABS_API_KEY": "elkey"}))
    assert "sekrit" not in text and "elkey" not in text and "123:abc" not in text
