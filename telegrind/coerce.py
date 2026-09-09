"""Turn model output into values a worksheet and Postgres both accept.

The registry's column type decides which coercer runs. Every coercer must
tolerate a *missing* value, not just an empty one: structured outputs allows
a partial `required` list, so an optional column arrives as an absent key.
"""

import logging
import re
from datetime import datetime

import dateparser
from pydantic import TypeAdapter, ValidationError
from pydantic_extra_types.currency_code import Currency

from telegrind.registry import Category
from telegrind.sheets import Config

log = logging.getLogger(__name__)

SHEET_DATETIME_FORMAT = "%d.%m.%y %H:%M"

_NOT_NUMERIC = re.compile(r"[^0-9.\-]")
_CURRENCY_ADAPTER = TypeAdapter(Currency)


def coerce_number(raw: object) -> float | str:
    """A float, or `""` when there is nothing numeric to read."""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int | float):
        return float(raw)
    text = str(raw).strip().replace(",", ".")
    text = _NOT_NUMERIC.sub("", text)
    try:
        return float(text)
    except ValueError:
        log.debug("cannot read a number from %r", raw)
        return ""


def coerce_currency(raw: object, cfg: Config) -> str:
    """An ISO 4217 code, falling back to the workbook's configured currency."""
    if raw:
        code = str(raw).strip().upper()
        try:
            return str(_CURRENCY_ADAPTER.validate_python(code))
        except ValidationError:
            log.debug("%r is not an ISO 4217 code, using %s", raw, cfg.currency)
    return str(cfg.currency).upper()


def coerce_datetime(
    raw: object,
    cfg: Config,
    fallback: datetime,
    *,
    prefer_future: bool = False,
) -> str:
    """ISO first, then dateparser, then the message's own timestamp.

    `prefer_future` is what separates `due` from `date`: an ambiguous
    reference like "во вторник" resolves forward for a due column and
    backward for a date column.
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
                    "TIMEZONE": cfg.tzname,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future" if prefer_future else "past",
                    "RELATIVE_BASE": fallback.replace(tzinfo=None),
                },
            )

    if parsed is None:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=cfg.tz)

    return cfg.localized(parsed).strftime(SHEET_DATETIME_FORMAT)


def coerce_fields(
    cat: Category,
    raw: dict[str, object],
    cfg: Config,
    fallback: datetime,
) -> dict[str, object]:
    """Coerce one fact's fields, keyed by header, in the category's column order.

    Fields the registry does not declare are dropped: the model is not
    allowed to invent a column, and column A is never a field.
    """
    out: dict[str, object] = {}
    for column in cat.columns:
        value = raw.get(column.header)
        match column.type:
            case "number" | "money":
                out[column.header] = coerce_number(value)
            case "currency":
                out[column.header] = coerce_currency(value, cfg)
            case "date":
                out[column.header] = coerce_datetime(value, cfg, fallback)
            case "due":
                out[column.header] = coerce_datetime(
                    value, cfg, fallback, prefer_future=True
                )
            case _:
                out[column.header] = "" if value is None else str(value).strip()
    return out
