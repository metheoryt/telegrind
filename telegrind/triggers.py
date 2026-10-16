"""What is a question (SPEC section 6). No model decides this for text."""

from __future__ import annotations

from telegrind.incoming import AUTHOR_ME

BARE_Q = object()
BARE_Q_TEXT = "Напиши вопрос после /q, например: /q сколько я потратил на такси в сентябре?"


def question_text(text: str | None, *, reply_to_author: str | None, author: str) -> str | object | None:
    """The question's text, BARE_Q for a bare /q, or None for a log entry (REQ-TRIGGER-1..3)."""
    if author != AUTHOR_ME or not text or not text.strip():
        return None
    stripped = text.strip()
    if stripped.startswith("/"):
        parts = stripped.split(maxsplit=1)
        if parts[0].split("@", 1)[0] == "/q":
            return parts[1].strip() if len(parts) > 1 else BARE_Q
        return stripped
    if reply_to_author == "bot" or stripped.endswith("?"):
        return stripped
    return None
