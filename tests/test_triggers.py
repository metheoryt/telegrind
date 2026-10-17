"""What is a question (SPEC section 6): a fixed rule, no model."""

import pytest

from telegrind.triggers import BARE_Q, question_text


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
        ("/q сколько такси", None, "сколько такси"),
        ("/q   ", None, BARE_Q),
        ("/q", None, BARE_Q),
        ("/q@telegrind_bot сколько такси", None, "сколько такси"),
        ("/q\nсколько такси", None, "сколько такси"),
        ("/q\tx", None, "x"),
        ("/q@bot\nx", None, "x"),
        ("", None, None),
        (None, None, None),
        (None, "bot", None),
    ],
)
def test_rule(text, reply_author, expected):
    """REQ-TRIGGER-1, REQ-TRIGGER-2: reply to the bot and leading / only — a trailing ? is a log entry;
    /q strips itself; bare /q is the sentinel."""
    assert question_text(text, reply_to_author=reply_author, author="me") == expected


def test_bot_and_forward_never_trigger():
    """REQ-TRIGGER-3, REQ-MSG-4: the bot's own text never triggers, nor does a forwarded command."""
    assert question_text("ok?", reply_to_author=None, author="bot") is None
    assert question_text("/q x", reply_to_author=None, author="bot") is None
    assert question_text("/start", reply_to_author=None, author="forward: Vasya") is None
