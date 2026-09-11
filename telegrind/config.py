"""Per-chat settings, read off the chat row.

These used to live in a `_config` worksheet. They are not spreadsheet
concerns: the timezone decides what date a fact gets and what «в августе»
means, and the currency is what an amount defaults to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegrind.models import Chat


@dataclass(frozen=True, slots=True)
class ChatConfig:
    tz_offset: int
    currency: str

    @classmethod
    def of(cls, chat: Chat) -> ChatConfig:
        return cls(tz_offset=chat.tz_offset, currency=chat.currency)

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.tz_offset))

    def localized(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)
