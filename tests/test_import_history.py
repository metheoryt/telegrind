from datetime import UTC, datetime

from telegrind import store
from telegrind.import_history import flatten, message_from, user_id, verdict_of
from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    LoggedMessage,
)

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


def row_for(entry: dict) -> LoggedMessage:
    """The LoggedMessage the importer would store, without a database."""
    msg = message_from(entry, chat_id=ME, bot_id=BOT)
    assert msg is not None
    return LoggedMessage(chat_pk=1, **store.message_values(msg))


def test_a_service_entry_is_not_a_message() -> None:
    assert (
        message_from(
            entry(type="service", action="pin_message"), chat_id=ME, bot_id=BOT
        )
        is None
    )


def test_the_id_and_the_date_survive() -> None:
    row = row_for(entry())
    assert row.message_id == 641715
    assert row.tg_date == datetime.fromtimestamp(1758000170, tz=UTC)
    assert row.kind == KIND_TEXT
    assert row.text == "449 usd apple watch"


def test_a_fragment_list_is_flattened_into_the_text() -> None:
    row = row_for(entry(text=["купил ", {"type": "bold", "text": "кофе"}]))
    assert row.text == "купил кофе"


def test_an_edit_keeps_the_live_shape() -> None:
    """`edit_date` is an int in `raw` on the live path — mode="json" would
    serialize a datetime as a string and break the one claim this whole
    design rests on."""
    row = row_for(entry(edited_unixtime="1758000300"))
    assert row.edited_at == datetime.fromtimestamp(1758000300, tz=UTC)
    assert row.raw["edit_date"] == 1758000300


def test_the_bots_own_entry_reads_back_as_the_bots() -> None:
    row = row_for(entry(from_id=f"user{BOT}", text="💔"))
    assert store.by_the_bot(row) is True


def test_the_users_entry_does_not() -> None:
    assert store.by_the_bot(row_for(entry())) is False


def test_a_reply_reads_back_through_store() -> None:
    row = row_for(entry(reply_to_message_id=641700))
    assert store.reply_to(row) == 641700


def test_no_reply_reads_back_as_none() -> None:
    assert store.reply_to(row_for(entry())) is None


def test_a_voice_entry_is_a_voice_message_with_a_marked_file_id() -> None:
    row = row_for(
        entry(
            text="",
            media_type="voice_message",
            file="voice_messages/audio_1.ogg",
            duration_seconds=9,
        )
    )
    assert row.kind == KIND_VOICE
    assert row.audio_duration == 9
    assert row.audio_file_id.startswith("import:")


def test_an_entry_with_no_text_stores_no_text() -> None:
    assert row_for(entry(text="")).text is None
