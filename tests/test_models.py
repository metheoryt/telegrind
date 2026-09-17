from datetime import UTC, datetime

from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    SOURCE_TELEGRAM,
    Chat,
    Entry,
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


def test_entry_table_name() -> None:
    assert Entry.__tablename__ == "entry"


def test_the_message_no_longer_carries_derivation_state() -> None:
    """`message` is the verbatim log and nothing else.

    Two columns that can disagree about whether something gets extracted is
    the `extractable`-vs-`verdict` defect this repo already shipped, so the
    old ones must be gone, not merely unread.
    """
    columns = set(LoggedMessage.__table__.columns.keys())
    assert not columns & {
        "verdict",
        "extracted_at",
        "extract_model",
        "extract_prompt_version",
        "extract_error",
    }
    assert "receipt_emoji" in columns


def test_entry_carries_the_verdict_and_the_extraction_state() -> None:
    columns = set(Entry.__table__.columns.keys())
    assert {
        "chat_pk",
        "source",
        "external_id",
        "message_pk",
        "occurred_at",
        "content",
        "raw",
        "verdict",
        "extracted_at",
        "extract_model",
        "extract_prompt_version",
        "extract_error",
        "created_at",
    } <= columns


def test_a_side_loaded_entry_has_no_message() -> None:
    """The whole point: a fact need not come from Telegram."""
    assert Entry.__table__.c.message_pk.nullable is True
    assert Entry.__table__.c.content.nullable is True
    assert Entry.__table__.c.raw.nullable is True


def test_an_entry_is_unique_per_source_row() -> None:
    """This is what makes a re-import idempotent rather than doubling."""
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Entry.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_pk", "external_id", "source") in constraints


def test_one_entry_per_message() -> None:
    """Partial, because most entries have no message and NULLs collide badly."""
    index = next(i for i in Entry.__table__.indexes if i.name == "uq_entry_message_pk")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["message_pk"]
    assert index.dialect_options["postgresql"]["where"] is not None


def test_the_queue_has_an_index() -> None:
    index = next(i for i in Entry.__table__.indexes if i.name == "ix_entry_queue")
    assert [c.name for c in index.columns] == [
        "chat_pk",
        "verdict",
        "extracted_at",
        "occurred_at",
    ]


def test_a_new_entry_is_unextracted() -> None:
    row = Entry(
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id="2",
        occurred_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )
    assert row.extracted_at is None
    assert row.extract_error is None
    assert Entry.__table__.c.verdict.default.arg == "fact"


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
        "entry_pk",
        "seq",
        "kind",
        "at",
        "fields",
        "created_at",
        "updated_at",
        "deleted_at",
    } <= columns


def test_a_fact_hangs_off_an_entry() -> None:
    assert "message_pk" not in Fact.__table__.columns
    assert Fact.__table__.c.entry_pk.nullable is False
    target = next(iter(Fact.__table__.c.entry_pk.foreign_keys)).target_fullname
    assert target == "entry.id"


def test_fact_at_is_timezone_aware() -> None:
    assert Fact.__table__.c.at.type.timezone is True


def test_the_live_uniqueness_moved_to_the_new_column() -> None:
    """Created anew, never renamed: an index renamed onto a different column
    guards nothing and fails silently."""
    names = {i.name for i in Fact.__table__.indexes}
    assert "uq_fact_entry_pk_seq_live" in names
    assert "uq_fact_message_pk_seq_live" not in names
    index = next(
        i for i in Fact.__table__.indexes if i.name == "uq_fact_entry_pk_seq_live"
    )
    assert index.unique is True
    assert [c.name for c in index.columns] == ["entry_pk", "seq"]


def test_facts_are_indexed_for_the_query_that_answers_q() -> None:
    names = {i.name for i in Fact.__table__.indexes}
    assert {"ix_fact_chat_kind_at_live", "ix_fact_fields"} <= names


def test_extracted_facts_record_what_produced_them() -> None:
    assert Fact.__table__.c.model.nullable is True
    assert Fact.__table__.c.prompt_version.nullable is True


def test_a_fact_can_hold_a_real_json_number() -> None:
    fact = Fact(
        chat_pk=1,
        entry_pk=1,
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
