"""The write boundary for fact field values.

Everything a fact knows lives in a JSONB column, so aggregation reads it
back with an expression like `(fields->>'amount')::numeric`. That does not
fail one row, it fails the whole query — and adding a generated column
later fails on the first non-numeric value in the table. So a value is
coerced once, here, on the way in: what parses becomes a real JSON number,
what does not stays text and simply never aggregates.

Nothing written is ever lost. That includes a number the model could not
pin down.
"""

from datetime import datetime

import dateparser

from telegrind.config import ChatConfig


def to_json_value(raw: object) -> object:
    """Return a JSON-safe value, with numbers as real numbers."""
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        return raw
    if not isinstance(raw, str):
        return raw

    text = raw.strip()
    if not text:
        return raw
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return raw


def to_instant(
    raw: object,
    cfg: ChatConfig,
    fallback: datetime,
    *,
    prefer_future: bool = False,
) -> datetime:
    """ISO first, then dateparser, then the message's own timestamp.

    This is how a fact gets its `at`. The fallback is the *message's*
    timestamp, never the moment of extraction: a batch pass can run a day
    after the message, and «вчера» must still mean the day before the
    message was written.

    `prefer_future` is what separates a due date from an event date: an
    ambiguous «во вторник» resolves forward for the first and backward for
    the second.
    """
    parsed: datetime | None = None

    if raw not in (None, ""):
        text = str(raw).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = dateparser.parse(
                text,
                languages=["ru", "en"],
                settings={
                    "TIMEZONE": f"{cfg.tz_offset:+03d}00",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future" if prefer_future else "past",
                    # The base is the chat's own wall clock, not UTC's.
                    # At 02:00 in Almaty it is still yesterday in UTC, and
                    # a naive UTC base made «сегодня» resolve a day behind
                    # what the person writing it meant.
                    "RELATIVE_BASE": cfg.localized(fallback).replace(tzinfo=None),
                },
            )

    if parsed is None:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=cfg.tz)
    return parsed
