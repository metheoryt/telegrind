from datetime import UTC, datetime

from telegrind.config import ChatConfig
from telegrind.extract import author_of, build_prompt
from telegrind.models import KIND_TEXT, LoggedMessage

CFG = ChatConfig(tz_offset=6, currency="KZT")
OWNER = 111


def logged(message_id: int, text: str, *, raw: dict | None = None) -> LoggedMessage:
    return LoggedMessage(
        id=message_id,
        chat_pk=1,
        message_id=message_id,
        kind=KIND_TEXT,
        text=text,
        tg_date=datetime(2026, 9, 11, 3, 0, tzinfo=UTC),
        raw=raw or {},
    )


def test_a_plain_message_is_the_owner_talking() -> None:
    assert author_of(logged(1, "4500 такси"), OWNER) == "я"


def test_a_forward_of_my_own_message_is_still_me() -> None:
    row = logged(
        1,
        "4500 такси",
        raw={"forward_origin": {"type": "user", "sender_user": {"id": OWNER}}},
    )

    assert author_of(row, OWNER) == "я"


def test_a_forward_from_someone_else_names_them() -> None:
    row = logged(
        1,
        "верни 5000",
        raw={
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 222, "first_name": "Мама"},
            }
        },
    )

    assert author_of(row, OWNER) == "переслано от «Мама»"


def test_a_hidden_sender_is_its_own_third_case() -> None:
    row = logged(
        1,
        "верни 5000",
        raw={"forward_origin": {"type": "hidden_user", "sender_user_name": "Мама"}},
    )

    assert "неизвестно" in author_of(row, OWNER)


def test_a_channel_forward_names_the_channel() -> None:
    row = logged(
        1,
        "новый монитор",
        raw={"forward_origin": {"type": "channel", "chat": {"title": "Техника"}}},
    )

    assert author_of(row, OWNER) == "переслано из канала «Техника»"


def test_a_reply_names_its_parent_by_marker() -> None:
    prompt = build_prompt(
        tail=[
            logged(10, "макбук за 660000"),
            logged(11, "чек", raw={"reply_to_message": {"message_id": 10}}),
        ],
        context=[],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [1]" in prompt


def test_a_reply_to_a_context_message_names_its_context_marker() -> None:
    prompt = build_prompt(
        tail=[logged(11, "чек", raw={"reply_to_message": {"message_id": 9}})],
        context=[logged(9, "макбук за 660000")],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "ответ на [C1]" in prompt


def test_a_reply_to_something_outside_the_window_says_so() -> None:
    prompt = build_prompt(
        tail=[logged(11, "чек", raw={"reply_to_message": {"message_id": 3}})],
        context=[],
        taxonomy="",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "вне окна" in prompt


def test_the_prompt_numbers_the_tail_and_labels_the_context() -> None:
    prompt = build_prompt(
        tail=[logged(10, "4500 такси"), logged(11, "и ещё 300 кофе")],
        context=[logged(9, "вес 82.4")],
        taxonomy="- expense (5): amount, comment",
        cfg=CFG,
        chat_id=OWNER,
    )

    assert "[1]" in prompt and "[2]" in prompt
    assert "[C1]" in prompt
    # The chat's wall clock, not UTC's: 03:00 UTC is 09:00 in Almaty.
    assert "2026-09-11 09:00" in prompt
    assert "- expense (5): amount, comment" in prompt
    assert "KZT" in prompt
