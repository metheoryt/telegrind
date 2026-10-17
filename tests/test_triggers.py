"""What is a question (SPEC section 6): a fixed rule, no model."""

import pytest

from telegrind.triggers import question_text


@pytest.mark.parametrize(
    "text, reply_author, expected",
    [
        ("4500 такси", None, None),
        ("сколько я потратил на такси?", None, None),
        ("давление 120/80, нормально?", None, None),
        ("а это что", "bot", "а это что"),
        ("а это что", "me", None),
        ("/start", None, "/start"),
        ("/help me", None, "/help me"),
        ("/q сколько такси", None, "/q сколько такси"),
        ("/q", None, "/q"),
        ("", None, None),
        (None, None, None),
        (None, "bot", None),
    ],
)
def test_rule(text, reply_author, expected):
    """REQ-TRIGGER-1, REQ-TRIGGER-2: reply to the bot and leading / only — a trailing ? is a log entry;
    every command, /q included, is a question with its whole text."""
    assert question_text(text, reply_to_author=reply_author, author="me") == expected


def test_bot_and_forward_never_trigger():
    """REQ-TRIGGER-3, REQ-MSG-4: the bot's own text never triggers, nor does a forwarded command."""
    assert question_text("ok?", reply_to_author=None, author="bot") is None
    assert question_text("/q x", reply_to_author=None, author="bot") is None
    assert question_text("/start", reply_to_author=None, author="forward: Vasya") is None
