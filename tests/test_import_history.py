import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegrind import store
from telegrind.import_history import (
    Export,
    ExportMismatch,
    flatten,
    import_entries,
    message_from,
    read_export,
    user_id,
    verdict_of,
)
from telegrind.models import (
    KIND_TEXT,
    KIND_VOICE,
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    Chat,
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


def export_file(tmp_path: Path, **overrides: object) -> Path:
    doc: dict = {
        "name": "Aicha",
        "type": "bot_chat",
        "id": BOT,
        "messages": [
            entry(id=1, date_unixtime="1686000000"),
            entry(id=2, date_unixtime="1758000170", from_id=f"user{BOT}", text="💔"),
            {"id": 3, "type": "service", "action": "pin_message"},
        ],
    }
    doc.update(overrides)
    path = tmp_path / "result.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_the_bot_id_is_the_top_level_id(tmp_path: Path) -> None:
    """In a private export the top-level id is the PEER's — the bot's —
    while our own chat_id is the user's. Deriving one from the other is
    how the whole import lands under the wrong chat."""
    assert read_export(export_file(tmp_path), chat_id=ME).bot_id == BOT


def test_service_entries_are_dropped(tmp_path: Path) -> None:
    got = read_export(export_file(tmp_path), chat_id=ME)
    assert [e["id"] for e in got.entries] == [1, 2]


def test_a_third_author_refuses_to_run(tmp_path: Path) -> None:
    path = export_file(
        tmp_path,
        messages=[entry(id=1), entry(id=2, from_id="user999", **{"from": "Someone"})],
    )
    with pytest.raises(ExportMismatch, match="999"):
        read_export(path, chat_id=ME)


def test_a_group_export_refuses_to_run(tmp_path: Path) -> None:
    with pytest.raises(ExportMismatch, match="private_supergroup"):
        read_export(export_file(tmp_path, type="private_supergroup"), chat_id=ME)


def test_since_drops_the_earlier_entries(tmp_path: Path) -> None:
    got = read_export(
        export_file(tmp_path),
        chat_id=ME,
        since=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert [e["id"] for e in got.entries] == [2]


class FakeSession:
    """Enough of a session for a driver that only commits."""

    def __init__(self) -> None:
        self.commits = 0

    def begin(self) -> Any:
        @contextlib.asynccontextmanager
        async def ctx() -> Any:
            yield None
            self.commits += 1

        return ctx()


def recording_upsert() -> tuple[list[tuple[int, str]], Any]:
    calls: list[tuple[int, str]] = []

    async def upsert(session: Any, chat: Any, msg: Any, *, verdict: str) -> Any:
        calls.append((msg.message_id, verdict))
        return SimpleNamespace(id=len(calls)), True

    return calls, upsert


CHAT = Chat(id=1, chat_id=ME, tz_offset=6, currency="KZT")


async def test_every_entry_is_stored_with_an_explicit_verdict() -> None:
    """The default is VERDICT_FACT, and leaning on it here would feed
    2766 bot replies to the extractor."""
    calls, upsert = recording_upsert()
    export = Export(
        bot_id=BOT,
        entries=[
            entry(id=1),
            entry(id=2, from_id=f"user{BOT}", text="💔"),
            entry(id=3, text="/start"),
            entry(id=4, text="/q сколько"),
            entry(id=5, text="-"),
        ],
    )
    await import_entries(FakeSession(), CHAT, export, upsert=upsert)
    assert calls == [
        (1, VERDICT_FACT),
        (2, VERDICT_SYSTEM),
        (3, VERDICT_SYSTEM),
        (4, VERDICT_QUESTION),
        (5, VERDICT_SYSTEM),
    ]


async def test_the_report_counts_what_happened() -> None:
    _, upsert = recording_upsert()
    export = Export(bot_id=BOT, entries=[entry(id=1), entry(id=2, from_id="channel7")])
    report = await import_entries(FakeSession(), CHAT, export, upsert=upsert)
    assert report.seen == 2
    assert report.stored == 1
    assert report.skipped == 1
    assert report.verdicts[VERDICT_FACT] == 1


async def test_it_commits_once_per_chunk() -> None:
    _, upsert = recording_upsert()
    session = FakeSession()
    export = Export(bot_id=BOT, entries=[entry(id=i) for i in range(1, 6)])
    await import_entries(session, CHAT, export, upsert=upsert, chunk=2)
    assert session.commits == 3


async def test_a_dry_run_stores_nothing() -> None:
    calls, upsert = recording_upsert()
    session = FakeSession()
    export = Export(bot_id=BOT, entries=[entry(id=1)])
    report = await import_entries(session, CHAT, export, upsert=upsert, dry_run=True)
    assert calls == []
    assert session.commits == 0
    assert report.seen == 1
    assert report.stored == 0


async def test_the_report_counts_forwards() -> None:
    """171 entries are forwards. They lose their origin date and reach the
    extractor as the user's own words, and neither is reconstructable from
    the export — so the spec asks for a count, which is the whole remedy."""
    _, upsert = recording_upsert()
    export = Export(
        bot_id=BOT,
        entries=[entry(id=1), entry(id=2, forwarded_from="Someone")],
    )
    report = await import_entries(FakeSession(), CHAT, export, upsert=upsert)
    assert report.forwards == 1
