from telegrind.import_history import flatten, user_id, verdict_of
from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, VERDICT_SYSTEM

BOT = 6039253940
ME = 3260987


def entry(**overrides: object) -> dict:
    base: dict = {
        "id": 641715,
        "type": "message",
        "date": "2025-09-16T10:02:50",
        "date_unixtime": "1758000170",
        "from": "Maxim",
        "from_id": f"user{ME}",
        "text": "449 usd apple watch",
    }
    base.update(overrides)
    return base


def test_flatten_passes_a_plain_string_through() -> None:
    assert flatten("4500 такси") == "4500 такси"


def test_flatten_joins_the_fragment_list() -> None:
    """350 of the 6837 entries carry formatting and arrive as a list."""
    raw = ["купил ", {"type": "bold", "text": "кофе"}, " 1570"]
    assert flatten(raw) == "купил кофе 1570"


def test_flatten_of_nothing_is_empty() -> None:
    assert flatten(None) == ""
    assert flatten([]) == ""


def test_user_id_strips_the_prefix() -> None:
    assert user_id(entry()) == ME


def test_user_id_of_a_non_user_author_is_none() -> None:
    """A channel or a service entry has no `user<N>` to read."""
    assert user_id(entry(from_id="channel1234")) is None
    assert user_id(entry(from_id=None)) is None


def test_the_bots_own_messages_are_system() -> None:
    assert verdict_of(entry(from_id=f"user{BOT}"), bot_id=BOT) == VERDICT_SYSTEM


def test_a_bare_dash_is_system() -> None:
    """56 of these: v1's delete marker, history and not a fact."""
    assert verdict_of(entry(text="-"), bot_id=BOT) == VERDICT_SYSTEM
    assert verdict_of(entry(text=" - "), bot_id=BOT) == VERDICT_SYSTEM


def test_a_command_is_system() -> None:
    assert verdict_of(entry(text="/start"), bot_id=BOT) == VERDICT_SYSTEM
    assert verdict_of(entry(text="/link http://x"), bot_id=BOT) == VERDICT_SYSTEM


def test_q_is_a_question() -> None:
    assert verdict_of(entry(text="/q сколько потратил"), bot_id=BOT) == VERDICT_QUESTION
    assert verdict_of(entry(text="/q@aichabot сколько"), bot_id=BOT) == VERDICT_QUESTION


def test_ordinary_text_is_a_fact() -> None:
    assert verdict_of(entry(), bot_id=BOT) == VERDICT_FACT
    assert (
        verdict_of(entry(text=["1570 ", {"text": "кофе"}]), bot_id=BOT) == VERDICT_FACT
    )


def test_an_entry_with_no_text_is_still_a_fact() -> None:
    """A photo is stored like everything else and waits; `_has_content()`
    keeps it out of the tail, so the verdict need not lie about it."""
    assert verdict_of(entry(text=""), bot_id=BOT) == VERDICT_FACT
