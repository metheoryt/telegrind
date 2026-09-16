"""One-time import of the v1 history from a Telegram Desktop export.

Run from a shell, not from the chat. Every decision it makes is a fact
about the export entry — who sent it, whether it starts with a slash —
rather than a guess, which is why `classify` is never called: the
classifier exists because a live message arrives without that structure,
and 3915 model calls would buy nothing here.

Deleted or frozen once it has run. It is one file for the same reason.
"""

import logging
from typing import Any

from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, VERDICT_SYSTEM

log = logging.getLogger(__name__)

#: v1's delete marker. 56 of them in the corpus.
DASH = "-"


def flatten(raw: Any) -> str:
    """The export's `text`, as a string.

    Telegram Desktop writes a plain string for unformatted text and a
    list of fragments — strings and `{"type": ..., "text": ...}` dicts —
    for anything with a link, a bold run or a code span. 350 entries in
    this corpus take the second shape, so it is not an edge case.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    return "".join(
        part if isinstance(part, str) else str(part.get("text", "")) for part in raw
    )


def user_id(entry: dict) -> int | None:
    """The author's Telegram id, or None if the entry has no user author.

    `from_id` is `user<N>` for a person or a bot; a channel post reads
    `channel<N>` and a service entry has no `from_id` at all.
    """
    raw = entry.get("from_id")
    if not isinstance(raw, str) or not raw.startswith("user"):
        return None
    return int(raw.removeprefix("user"))


def verdict_of(entry: dict, *, bot_id: int) -> str:
    """What this entry is, decided from its structure.

    Four arms, matching `telegrind/classify.py`'s vocabulary. `talk` is
    not among them: the classifier reaches it for a reply into a
    conversation, and 57 of the user's 4069 messages are replies, none of
    them to the bot — so asserting `fact` is wrong zero times here.
    """
    if user_id(entry) == bot_id:
        return VERDICT_SYSTEM
    text = flatten(entry.get("text")).strip()
    if text == DASH:
        return VERDICT_SYSTEM
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@")[0]
        return VERDICT_QUESTION if command == "/q" else VERDICT_SYSTEM
    return VERDICT_FACT
