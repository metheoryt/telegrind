"""Configuration: the environment and nothing else (SPEC REQ-DEPLOY-3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """A variable missing or malformed; the bot refuses to start."""


def _int(env: Mapping[str, str], name: str, default: int, *, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def _secret(env: Mapping[str, str], name: str) -> str | None:
    return env.get(name, "").strip() or None


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    claude_token: str | None = field(repr=False)
    elevenlabs_key: str | None = field(repr=False)
    data_dir: Path
    account_home: Path
    default_tz: str
    quiet_hour: int
    backlog_limit: int
    turn_quota: int
    max_concurrent: int
    pass_timeout_s: float
    question_timeout_s: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        token = env.get("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("BOT_TOKEN is required")
        raw_ids = env.get("ALLOWED_USER_IDS", "").strip()
        if not raw_ids:
            raise ConfigError(
                "ALLOWED_USER_IDS is required: a comma-separated list of Telegram user ids (REQ-ACCESS-1)"
            )
        try:
            ids = frozenset(int(part) for part in raw_ids.split(",") if part.strip())
        except ValueError:
            raise ConfigError(f"ALLOWED_USER_IDS must hold integers, got {raw_ids!r}") from None
        if not ids:
            raise ConfigError("ALLOWED_USER_IDS is empty")
        tz = env.get("DEFAULT_TZ", "").strip() or "Asia/Almaty"
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"DEFAULT_TZ is not an IANA zone: {tz!r}") from None
        return cls(
            bot_token=token,
            allowed_user_ids=ids,
            claude_token=_secret(env, "CLAUDE_CODE_OAUTH_TOKEN"),
            elevenlabs_key=_secret(env, "ELEVENLABS_API_KEY"),
            data_dir=Path(env.get("DATA_DIR", "").strip() or "/data/telegrind"),
            account_home=Path(env.get("ACCOUNT_HOME", "").strip() or "/claude"),
            default_tz=tz,
            quiet_hour=_int(env, "QUIET_HOUR", 4, low=0, high=23),
            backlog_limit=_int(env, "BACKLOG_LIMIT", 50, low=1, high=100_000),
            turn_quota=_int(env, "TURN_QUOTA", 50, low=0, high=100_000),
            max_concurrent=_int(env, "MAX_CONCURRENT", 2, low=1, high=64),
            pass_timeout_s=_float(env, "PASS_TIMEOUT_S", 600.0),
            question_timeout_s=_float(env, "QUESTION_TIMEOUT_S", 300.0),
        )
