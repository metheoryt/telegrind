from datetime import UTC, datetime

from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
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


def test_chat_carries_its_own_settings() -> None:
    assert {"tz_offset", "currency"} <= set(Chat.__table__.columns.keys())


def test_telegram_ids_are_bigints() -> None:
    assert LoggedMessage.__table__.c.message_id.type.__class__.__name__ == "BigInteger"


def test_message_is_unique_per_chat() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in LoggedMessage.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_pk", "message_id") in constraints


def test_message_carries_extraction_state() -> None:
    columns = set(LoggedMessage.__table__.columns.keys())
    assert {
        "extracted_at",
        "extract_model",
        "extract_prompt_version",
        "extractable",
        "extract_error",
    } <= columns


def test_a_new_message_is_extractable_and_unextracted() -> None:
    row = LoggedMessage(chat_pk=1, message_id=2, kind="text", raw={})
    assert row.extracted_at is None
    assert row.extract_error is None
    assert LoggedMessage.__table__.c.extractable.default.arg is True


def test_not_yet_extracted_is_distinct_from_extracted_and_empty() -> None:
    """The whole reason extracted_at is a column and not an inference.

    Having no fact rows cannot tell the two apart, and a batch pass that
    cannot tell them apart re-reads every silent message forever.
    """
    assert LoggedMessage.__table__.c.extracted_at.nullable is True


def test_fact_has_no_spreadsheet_coordinates() -> None:
    columns = set(Fact.__table__.columns.keys())
    assert "worksheet" not in columns
    assert "sheet_key" not in columns
    assert "origin" not in columns
    assert "category" not in columns


def test_fact_is_service_columns_plus_jsonb() -> None:
    columns = set(Fact.__table__.columns.keys())
    assert {
        "chat_pk",
        "message_pk",
        "seq",
        "kind",
        "at",
        "fields",
        "created_at",
        "updated_at",
        "deleted_at",
    } <= columns


def test_fact_requires_a_source_message() -> None:
    assert Fact.__table__.c.message_pk.nullable is False


def test_fact_at_is_timezone_aware() -> None:
    assert Fact.__table__.c.at.type.timezone is True


def test_the_live_uniqueness_is_partial_not_total() -> None:
    """A tombstoned fact keeps its (message_pk, seq).

    A total constraint would make re-extracting an edited message collide
    with the row it replaces.
    """
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Fact.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("message_pk", "seq") not in constraints

    live = next(
        i for i in Fact.__table__.indexes if i.name == "uq_fact_message_pk_seq_live"
    )
    assert live.unique is True
    assert live.dialect_options["postgresql"]["where"] is not None


def test_facts_are_indexed_for_the_query_that_answers_q() -> None:
    names = {i.name for i in Fact.__table__.indexes}
    assert {"ix_fact_chat_kind_at_live", "ix_fact_fields"} <= names


def test_extracted_facts_record_what_produced_them() -> None:
    assert Fact.__table__.c.model.nullable is True
    assert Fact.__table__.c.prompt_version.nullable is True


def test_a_fact_can_hold_a_real_json_number() -> None:
    fact = Fact(
        chat_pk=1,
        message_pk=1,
        seq=1,
        kind="expense",
        at=datetime(2026, 9, 9, tzinfo=UTC),
        fields={"amount": 4500, "currency": "KZT", "comment": "такси"},
    )
    assert isinstance(fact.fields["amount"], int)


def test_content_prefers_the_transcript() -> None:
    msg = LoggedMessage(kind=KIND_VOICE, text=None, transcript="вес 82.4")
    assert msg.content == "вес 82.4"


def test_content_falls_back_to_text() -> None:
    msg = LoggedMessage(kind=KIND_TEXT, text="4500 такси", transcript=None)
    assert msg.content == "4500 такси"


def test_content_is_empty_when_there_is_nothing() -> None:
    assert LoggedMessage(kind=KIND_TEXT).content == ""
