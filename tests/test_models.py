from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    ORIGIN_EXTRACTED,
    ORIGIN_IMPORTED,
    Chat,
    Fact,
    LoggedMessage,
)


def test_table_names() -> None:
    assert LoggedMessage.__tablename__ == "message"
    assert Fact.__tablename__ == "fact"


def test_chat_and_file_tables_are_untouched() -> None:
    assert Chat.__tablename__ == "chat"
    assert {"id", "chat_id", "sheet_url"} <= set(Chat.__table__.columns.keys())


def test_telegram_ids_are_bigints() -> None:
    assert LoggedMessage.__table__.c.message_id.type.__class__.__name__ == "BigInteger"


def test_message_is_unique_per_chat() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in LoggedMessage.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_pk", "message_id") in constraints


def test_fact_is_unique_per_message_and_seq() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Fact.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("message_pk", "seq") in constraints
    assert ("chat_pk", "sheet_key", "worksheet") in constraints


def test_fact_is_chat_scoped_even_without_a_message() -> None:
    """Imported facts have no message, so chat_pk carries the scoping."""
    assert Fact.__table__.c.chat_pk.nullable is False
    assert Fact.__table__.c.message_pk.nullable is True


def test_imported_facts_need_no_model_or_prompt_version() -> None:
    assert Fact.__table__.c.model.nullable is True
    assert Fact.__table__.c.prompt_version.nullable is True


def test_content_prefers_the_transcript() -> None:
    msg = LoggedMessage(kind=KIND_VOICE, text=None, transcript="вес 82.4")
    assert msg.content == "вес 82.4"


def test_content_falls_back_to_text() -> None:
    msg = LoggedMessage(kind=KIND_TEXT, text="4500 такси", transcript=None)
    assert msg.content == "4500 такси"


def test_content_is_empty_when_there_is_nothing() -> None:
    assert LoggedMessage(kind=KIND_TEXT).content == ""


def test_origin_constants() -> None:
    assert ORIGIN_EXTRACTED == "extracted"
    assert ORIGIN_IMPORTED == "imported"
