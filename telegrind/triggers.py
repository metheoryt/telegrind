"""What is a question (SPEC section 6). No model decides this, and a trailing ? decides nothing."""

from __future__ import annotations

from telegrind.incoming import AUTHOR_ME

ADDRESS = "❤"  # the person's reaction that addresses an entry to the bot (REQ-TRIGGER-4)
_VS16 = "\ufe0f"
WORDLESS_REPLY = "Человек ответил на твоё сообщение без текста. Посмотри, что он прислал, и ответь."


def holds_address(emoji: list[str]) -> bool:
    """REQ-TRIGGER-4: one of the reactions is ❤, with or without the variation selector."""
    return any(e.replace(_VS16, "") == ADDRESS for e in emoji)


def question_text(
    text: str | None, *, reply_to_author: str | None, author: str, wordless: bool = False
) -> str | None:
    """The question's text, or None for a log entry (REQ-TRIGGER-1..3).

    `wordless`: the entry has no text and never will (a photo, a document, a sticker — not a voice note waiting
    for its transcript); replying to the bot, it is asked with a fixed sentence (REQ-TRIGGER-1).
    A log entry may still be addressed later by ❤ (REQ-TRIGGER-4, App.on_reaction)."""
    if author != AUTHOR_ME:
        return None
    if not text or not text.strip():
        return WORDLESS_REPLY if wordless and reply_to_author == "bot" else None
    stripped = text.strip()
    if stripped.startswith("/") or reply_to_author == "bot":
        return stripped
    return None
