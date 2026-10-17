"""What is a question (SPEC section 6). No model decides this, and a trailing ? decides nothing."""

from __future__ import annotations

from telegrind.incoming import AUTHOR_ME

ADDRESS = "❤"  # the person's reaction that addresses an entry to the bot (REQ-TRIGGER-4)
_VS16 = "\ufe0f"


def holds_address(emoji: list[str]) -> bool:
    """REQ-TRIGGER-4: one of the reactions is ❤, with or without the variation selector."""
    return any(e.replace(_VS16, "") == ADDRESS for e in emoji)


def question_text(text: str | None, *, reply_to_author: str | None, author: str) -> str | None:
    """The question's text, or None for a log entry (REQ-TRIGGER-1..3).

    A log entry may still be addressed later by ❤ (REQ-TRIGGER-4, App.on_reaction)."""
    if author != AUTHOR_ME or not text or not text.strip():
        return None
    stripped = text.strip()
    if stripped.startswith("/") or reply_to_author == "bot":
        return stripped
    return None
