# v1 history import — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load three years of the v1 chat — 6837 entries from a Telegram Desktop
export — into v2's `message` table, then derive facts from it in batches, without
a single call through the bot's dispatcher.

**Architecture:** One module, `telegrind/import_history.py`, with two
subcommands. `import` turns each export entry into a real
`aiogram.types.Message` and hands it to the existing `store.upsert_message`, so
the `raw` JSONB column comes out the same shape live ingestion produces;
`verdict` is asserted from the entry's own structure and passed explicitly at
every call site, never classified. `extract` then drives `extract.run` in small
passes until the tail is empty. Everything that decides something is a pure
function tested without a database.

**Tech Stack:** Python 3.14, aiogram 3.x, SQLAlchemy 2 (asyncpg), pytest
(`asyncio_mode = auto`), ruff, ty.

**Spec:** `docs/superpowers/specs/2026-09-16-history-import-design.md`

## Global Constraints

- **`verdict` is passed explicitly on every `upsert_message` call.** Its default
  is `VERDICT_FACT`; leaning on the default is the `db9de98` trap, and here it
  would feed 2766 bot replies to the extractor.
- **The importer never touches `routing.route`, `outbound.say`, or any handler.**
  It calls `store.upsert_message` and commits.
- **Extraction passes are ~20 messages.** `extract._pass` makes one model call
  per pass and `llm.MAX_TOKENS` is 2048; a pass whose facts do not fit the reply
  fails whole.
- **`PROMPT_VERSION` is not bumped.** The prompt does not change meaning.
- **Owner's chat only** — `chat_id` 3260987, bot id 6039253940. v1's database,
  container and volume are not touched.
- **Never run a bare `uv sync`** — `uv sync -p /usr/bin/python3.14`.
- **Lint specifics:** `ANN` wants a return annotation on every function
  including tests and nested fakes; `T20` bans `print` outside `__main__`-facing
  code (the CLI writes through `logging`, not `print`); an unused `# noqa` is an
  error (`RUF100`), so write reasons as plain comments.
- Every task ends with `uv run pytest`, `uv run ruff check`, `uv run ruff format`
  and `uv run ty check` green, then a commit.

## File structure

| File | Responsibility |
|---|---|
| `telegrind/import_history.py` (create) | the whole importer: parsing the export, asserting verdicts, building messages, the two drivers, the CLI |
| `tests/test_import_history.py` (create) | everything above, without a database |
| `telegrind/workbook_compare.py` (create, Task 8) | the one-time workbook↔facts comparison report |
| `tests/test_workbook_compare.py` (create, Task 8) | its join and drop rules |
| `CLAUDE.md` (modify, Task 6) | one paragraph: the importer exists, what it is for, how it is run |

The importer is one file on purpose. It has one reason to exist and is deleted
or frozen after it has run; splitting it across modules would spread a one-time
operation through a package that has to keep working afterwards.

---

### Task 1: Reading an entry — text, author, verdict

**Files:**
- Create: `telegrind/import_history.py`
- Test: `tests/test_import_history.py`

**Interfaces:**
- Consumes: `telegrind.models.VERDICT_FACT`, `VERDICT_QUESTION`, `VERDICT_SYSTEM`
- Produces: `flatten(raw: Any) -> str`, `user_id(entry: dict) -> int | None`,
  `verdict_of(entry: dict, *, bot_id: int) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_import_history.py
from telegrind.import_history import flatten, user_id, verdict_of
from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, VERDICT_SYSTEM

BOT = 6039253940
ME = 3260987


def entry(**overrides: object) -> dict:
    base: dict = {
        "id": 641715,
        "type": "message",
        "date": "2026-09-16T10:02:50",
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
    assert verdict_of(entry(text=["1570 ", {"text": "кофе"}]), bot_id=BOT) == VERDICT_FACT


def test_an_entry_with_no_text_is_still_a_fact() -> None:
    """A photo is stored like everything else and waits; `_has_content()`
    keeps it out of the tail, so the verdict need not lie about it."""
    assert verdict_of(entry(text=""), bot_id=BOT) == VERDICT_FACT
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegrind.import_history'`

- [ ] **Step 3: Write the module head and the three functions**

```python
# telegrind/import_history.py
"""One-time import of the v1 history from a Telegram Desktop export.

Run from a shell, not from the chat. Every decision it makes is a fact
about the export entry — who sent it, whether it starts with a slash —
rather than a guess, which is why `classify` is never called: the
classifier exists because a live message arrives without that structure,
and 3915 model calls would buy nothing here.

Deleted or frozen once it has run. It is one file for the same reason.
"""

import logging
from typing import Any

from telegrind.models import VERDICT_FACT, VERDICT_QUESTION, VERDICT_SYSTEM

log = logging.getLogger(__name__)

#: v1's delete marker. 56 of them in the corpus.
DASH = "-"


def flatten(raw: Any) -> str:
    """The export's `text`, as a string.

    Telegram Desktop writes a plain string for unformatted text and a
    list of fragments — strings and `{"type": ..., "text": ...}` dicts —
    for anything with a link, a bold run or a code span. 350 entries in
    this corpus take the second shape, so it is not an edge case.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    return "".join(
        part if isinstance(part, str) else str(part.get("text", "")) for part in raw
    )


def user_id(entry: dict) -> int | None:
    """The author's Telegram id, or None if the entry has no user author.

    `from_id` is `user<N>` for a person or a bot; a channel post reads
    `channel<N>` and a service entry has no `from_id` at all.
    """
    raw = entry.get("from_id")
    if not isinstance(raw, str) or not raw.startswith("user"):
        return None
    return int(raw.removeprefix("user"))


def verdict_of(entry: dict, *, bot_id: int) -> str:
    """What this entry is, decided from its structure.

    Four arms, matching `telegrind/classify.py`'s vocabulary. `talk` is
    not among them: the classifier reaches it for a reply into a
    conversation, and 57 of the user's 4069 messages are replies, none of
    them to the bot — so asserting `fact` is wrong zero times here.
    """
    if user_id(entry) == bot_id:
        return VERDICT_SYSTEM
    text = flatten(entry.get("text")).strip()
    if text == DASH:
        return VERDICT_SYSTEM
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@")[0]
        return VERDICT_QUESTION if command == "/q" else VERDICT_SYSTEM
    return VERDICT_FACT
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Prove the verdict tests discriminate**

Temporarily change `verdict_of` to `return VERDICT_FACT` unconditionally. Run the
file again. Expected: `test_the_bots_own_messages_are_system`,
`test_a_bare_dash_is_system`, `test_a_command_is_system` and `test_q_is_a_question`
fail — four failures, each an assertion about the verdict and not an import
error. Restore the function.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format telegrind/import_history.py tests/test_import_history.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/import_history.py tests/test_import_history.py
git commit -m "feat: read an export entry's text, author and verdict"
```

---

### Task 2: An export entry becomes an aiogram message

**Files:**
- Modify: `telegrind/import_history.py`
- Test: `tests/test_import_history.py`

**Interfaces:**
- Consumes: `flatten`, `user_id` from Task 1
- Produces: `message_from(entry: dict, *, chat_id: int, bot_id: int) -> Message | None`

- [ ] **Step 1: Write the failing tests**

These assert through `store`'s own readers, not through the dict. That is the
point of the task: a row whose `raw` has a different shape inserts fine and then
misattributes every imported line in the extraction window.

```python
# append to tests/test_import_history.py
from datetime import UTC, datetime

from telegrind import store
from telegrind.import_history import message_from
from telegrind.models import KIND_TEXT, KIND_VOICE, LoggedMessage


def row_for(entry: dict) -> LoggedMessage:
    """The LoggedMessage the importer would store, without a database."""
    msg = message_from(entry, chat_id=ME, bot_id=BOT)
    assert msg is not None
    return LoggedMessage(chat_pk=1, **store.message_values(msg))


def test_a_service_entry_is_not_a_message() -> None:
    assert message_from(entry(type="service", action="pin_message"), chat_id=ME, bot_id=BOT) is None


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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'message_from'`

- [ ] **Step 3: Implement `message_from`**

```python
# add to telegrind/import_history.py
from datetime import UTC, datetime

from aiogram.types import Chat as TgChat
from aiogram.types import Message, User, Voice


def _stub_reply(message_id: int, *, chat_id: int, date: datetime) -> Message:
    """The parent, as much of it as anything ever reads.

    `store.reply_to` reads `raw["reply_to_message"]["message_id"]` and
    nothing else — Telegram does not nest a second hop, which is why the
    column is read rather than the object. A stub carrying the id is
    therefore complete, not partial. Its date is the child's: an aiogram
    `Message` requires one, and no reader of this object looks at it.
    """
    return Message(message_id=message_id, date=date, chat=TgChat(id=chat_id, type="private"))


def message_from(entry: dict, *, chat_id: int, bot_id: int) -> Message | None:
    """One export entry as the aiogram object live ingestion would see.

    Returns None for anything that is not a user-or-bot message: the two
    service entries, and any future shape with no `user<N>` author.

    Built as an aiogram object rather than as column values because
    `store.message_values` dumps it into `raw`, and three readers parse
    that column afterwards — `store.by_the_bot`, `store.reply_to` and
    `extract.author_of`. Going through the same type is what makes the
    shape identical by construction instead of by agreement.
    """
    if entry.get("type") != "message":
        return None
    author = user_id(entry)
    if author is None:
        return None

    date = datetime.fromtimestamp(int(entry["date_unixtime"]), tz=UTC)
    edited = entry.get("edited_unixtime")
    reply = entry.get("reply_to_message_id")

    voice = None
    if entry.get("media_type") == "voice_message":
        # The export ships the audio but not Telegram's file id, and
        # `aiogram.types.Voice` requires one. The `import:` prefix is the
        # signal to whatever wires transcription later that this id
        # cannot be fetched — a placeholder that looks fetchable is worse
        # than an obvious one.
        voice = Voice(
            file_id=f"import:{entry.get('file', '')}",
            file_unique_id=f"import:{entry['id']}",
            duration=int(entry.get("duration_seconds") or 0),
        )

    return Message(
        message_id=int(entry["id"]),
        date=date,
        # The raw int, not a datetime: `message_values` dumps with
        # mode="json", where a datetime becomes an ISO string and the live
        # path leaves an int.
        edit_date=int(edited) if edited else None,
        chat=TgChat(id=chat_id, type="private"),
        from_user=User(
            id=author,
            is_bot=author == bot_id,
            first_name=str(entry.get("from") or "?"),
        ),
        text=flatten(entry.get("text")) or None,
        voice=voice,
        reply_to_message=(
            _stub_reply(int(reply), chat_id=chat_id, date=date) if reply else None
        ),
    )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: PASS, 22 tests.

- [ ] **Step 5: Prove the `raw`-shape tests discriminate**

Temporarily replace `is_bot=author == bot_id` with `is_bot=False`. Expected:
`test_the_bots_own_entry_reads_back_as_the_bots` fails on the assertion. Restore
it, then temporarily pass `edit_date=datetime.fromtimestamp(int(edited), tz=UTC)`;
expected: `test_an_edit_keeps_the_live_shape` fails on `row.raw["edit_date"]`
being a string. Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format telegrind/import_history.py tests/test_import_history.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/import_history.py tests/test_import_history.py
git commit -m "feat: an export entry becomes the aiogram message live ingestion sees"
```

---

### Task 3: Loading the export, and refusing the wrong one

**Files:**
- Modify: `telegrind/import_history.py`
- Test: `tests/test_import_history.py`

**Interfaces:**
- Consumes: `user_id` from Task 1
- Produces: `class Export(bot_id: int, entries: list[dict])`,
  `read_export(path: Path, *, chat_id: int, since: datetime | None = None) -> Export`,
  `class ExportMismatch(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_import_history.py
import json
from pathlib import Path

import pytest

from telegrind.import_history import ExportMismatch, read_export


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
        since=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert [e["id"] for e in got.entries] == [2]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExportMismatch'`

- [ ] **Step 3: Implement the loader**

```python
# add to telegrind/import_history.py
import json
from dataclasses import dataclass
from pathlib import Path

#: The only export type this reads. A group export has many authors and
#: no per-chat meaning for `chat_id`.
BOT_CHAT = "bot_chat"


class ExportMismatch(Exception):
    """The export is not the one chat this import is for."""


@dataclass(frozen=True, slots=True)
class Export:
    bot_id: int
    entries: list[dict]


def read_export(
    path: Path, *, chat_id: int, since: datetime | None = None
) -> Export:
    """The export's message entries, and who the bot is.

    Refuses rather than guesses. A third author means this is not the
    two-party chat the caller named, and importing it would file someone
    else's messages under `chat_id`.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("type") != BOT_CHAT:
        raise ExportMismatch(f"not a bot chat export: type={doc.get('type')!r}")

    bot_id = int(doc["id"])
    entries = [e for e in doc["messages"] if e.get("type") == "message"]

    authors = {user_id(e) for e in entries} - {None}
    unexpected = authors - {bot_id, chat_id}
    if unexpected:
        raise ExportMismatch(
            f"unexpected authors {sorted(unexpected)}; expected only "
            f"{chat_id} and {bot_id}"
        )

    if since is not None:
        cutoff = since.timestamp()
        entries = [e for e in entries if float(e["date_unixtime"]) >= cutoff]

    return Export(bot_id=bot_id, entries=entries)
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: PASS, 27 tests.

- [ ] **Step 5: Run it against the real export**

```bash
uv run python -c "
from datetime import UTC
from pathlib import Path
from telegrind.import_history import read_export
e = read_export(Path('/home/me/Загрузки/Telegram Desktop/ChatExport_2026-09-16/result.json'), chat_id=3260987)
print(e.bot_id, len(e.entries))
"
```

Expected: `6039253940 6835`. If the count differs from 6835 or the call raises,
stop and read the export before touching anything else — the corpus was measured
on 2026-09-16 and a disagreement means the file changed.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format telegrind/import_history.py tests/test_import_history.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/import_history.py tests/test_import_history.py
git commit -m "feat: read the export, and refuse one that is not this chat"
```

---

### Task 4: The import driver

**Files:**
- Modify: `telegrind/import_history.py`
- Test: `tests/test_import_history.py`

**Interfaces:**
- Consumes: `Export`, `message_from`, `verdict_of`
- Produces: `class ImportReport(seen, stored, skipped, verdicts)`,
  `async def import_entries(session, chat, export, *, upsert=store.upsert_message, chunk=500) -> ImportReport`

The `upsert` parameter is injected the way `extract.run` injects `call`: the
driver is then testable without a database, and the test can assert the exact
`verdict` passed for every entry — which is the thing most worth asserting.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_import_history.py
import contextlib
from types import SimpleNamespace
from typing import Any

from telegrind.import_history import Export, import_entries
from telegrind.models import Chat


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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'import_entries'`

- [ ] **Step 3: Implement the driver**

```python
# add to telegrind/import_history.py
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.models import Chat

#: Rows per transaction. Small enough that a failure costs one chunk,
#: large enough that 6835 entries are not 6835 commits.
CHUNK = 500


@dataclass(frozen=True, slots=True)
class ImportReport:
    seen: int
    stored: int
    skipped: int
    verdicts: Counter


async def import_entries(
    session: AsyncSession,
    chat: Chat,
    export: Export,
    *,
    upsert: Callable[..., Awaitable[Any]] = store.upsert_message,
    chunk: int = CHUNK,
    dry_run: bool = False,
) -> ImportReport:
    """Store every entry, one transaction per chunk.

    Nothing here routes, reacts or replies. Going through the dispatcher
    would put a 💔 on three thousand historical messages and answer every
    question in the log into a live chat.

    Idempotent by construction: `upsert_message` overwrites on
    `(chat_pk, message_id)` and clears `extracted_at`, so a re-run
    re-queues exactly what changed.
    """
    seen = stored = skipped = 0
    verdicts: Counter = Counter()

    for start in range(0, len(export.entries), chunk):
        batch = export.entries[start : start + chunk]
        if dry_run:
            for item in batch:
                seen += 1
                if message_from(item, chat_id=chat.chat_id, bot_id=export.bot_id) is None:
                    skipped += 1
                else:
                    verdicts[verdict_of(item, bot_id=export.bot_id)] += 1
            continue

        async with session.begin():
            for item in batch:
                seen += 1
                msg = message_from(item, chat_id=chat.chat_id, bot_id=export.bot_id)
                if msg is None:
                    skipped += 1
                    continue
                verdict = verdict_of(item, bot_id=export.bot_id)
                await upsert(session, chat, msg, verdict=verdict)
                verdicts[verdict] += 1
                stored += 1

        log.info("imported %s/%s entries", seen, len(export.entries))

    return ImportReport(seen=seen, stored=stored, skipped=skipped, verdicts=verdicts)
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: PASS, 31 tests.

- [ ] **Step 5: Prove the verdict test discriminates**

Temporarily drop `verdict=verdict` from the `upsert` call and give the fake's
`verdict` parameter the real default (`verdict: str = VERDICT_FACT`). Expected:
`test_every_entry_is_stored_with_an_explicit_verdict` fails with four of the five
tuples reading `fact`. That failure is the `db9de98` trap reproduced in a test.
Restore.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format telegrind/import_history.py tests/test_import_history.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/import_history.py tests/test_import_history.py
git commit -m "feat: the import driver, with the verdict passed at every call"
```

---

### Task 5: The extraction driver

**Files:**
- Modify: `telegrind/import_history.py`
- Test: `tests/test_import_history.py`

**Interfaces:**
- Consumes: `telegrind.extract.run`, `telegrind.config.ChatConfig`
- Produces: `async def extract_all(session, chat, *, batch=BATCH, max_passes=None, run=extract.run) -> list[extract.Report]`,
  `BATCH = 20`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_import_history.py
from telegrind.extract import Report
from telegrind.import_history import BATCH, extract_all


def test_the_batch_is_small_enough_for_max_tokens() -> None:
    """`extract._pass` makes ONE model call for the whole tail and
    `llm.MAX_TOKENS` is 2048, so an oversized pass is not a slow pass —
    the reply truncates and the batch fails whole."""
    from telegrind import llm

    assert BATCH <= 25
    assert llm.MAX_TOKENS == 2048


async def test_it_passes_until_the_tail_is_empty() -> None:
    reports = [
        Report(pending=20, extracted=20, facts=25, failed=0, complaints=0),
        Report(pending=7, extracted=7, facts=9, failed=0, complaints=1),
        Report(pending=0, extracted=0, facts=0, failed=0, complaints=0),
    ]
    seen: list[int] = []

    async def fake_run(session: Any, chat: Any, cfg: Any, *, limit: int, **kw: Any) -> Report:
        seen.append(limit)
        return reports[len(seen) - 1]

    got = await extract_all(FakeSession(), CHAT, run=fake_run)
    assert seen == [BATCH, BATCH, BATCH]
    assert [r.extracted for r in got] == [20, 7, 0]


async def test_it_stops_at_max_passes() -> None:
    async def fake_run(session: Any, chat: Any, cfg: Any, *, limit: int, **kw: Any) -> Report:
        return Report(pending=20, extracted=20, facts=1, failed=0, complaints=0)

    got = await extract_all(FakeSession(), CHAT, max_passes=3, run=fake_run)
    assert len(got) == 3


async def test_a_failed_pass_stops_the_run() -> None:
    """A failed pass leaves its tail pending, so continuing would retry
    the same twenty messages forever."""

    async def fake_run(session: Any, chat: Any, cfg: Any, *, limit: int, **kw: Any) -> Report:
        return Report(pending=20, extracted=0, facts=0, failed=20, complaints=0)

    got = await extract_all(FakeSession(), CHAT, run=fake_run)
    assert len(got) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'BATCH'`

- [ ] **Step 3: Implement the driver**

```python
# add to telegrind/import_history.py
from telegrind import extract
from telegrind.config import ChatConfig

#: Messages per extraction pass. `extract._pass` makes one model call for
#: the whole tail and `llm.MAX_TOKENS` is 2048 — roughly forty facts —
#: so this is a correctness bound, not a throughput knob. The default
#: limit of 200 would truncate the reply and fail the batch whole.
BATCH = 20


async def extract_all(
    session: AsyncSession,
    chat: Chat,
    *,
    batch: int = BATCH,
    max_passes: int | None = None,
    run: Callable[..., Awaitable[extract.Report]] = extract.run,
) -> list[extract.Report]:
    """Drive passes until the tail is empty, one transaction each.

    Stops on the first failed pass rather than retrying: `_pass` leaves a
    failed tail pending, so a loop that continued would ask the same
    twenty messages again until the money ran out.
    """
    cfg = ChatConfig.of(chat)
    reports: list[extract.Report] = []

    while max_passes is None or len(reports) < max_passes:
        async with session.begin():
            report = await run(session, chat, cfg, limit=batch)
        reports.append(report)
        log.info(
            "pass %s: extracted=%s facts=%s failed=%s complaints=%s",
            len(reports),
            report.extracted,
            report.facts,
            report.failed,
            report.complaints,
        )
        if report.failed or report.pending == 0:
            break

    return reports
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: PASS, 35 tests.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff format telegrind/import_history.py tests/test_import_history.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/import_history.py tests/test_import_history.py
git commit -m "feat: drive extraction in passes small enough to fit the reply"
```

---

### Task 6: The CLI, and the chat row

**Files:**
- Modify: `telegrind/import_history.py`
- Modify: `CLAUDE.md`
- Test: `tests/test_import_history.py`

**Interfaces:**
- Consumes: everything above
- Produces: `async def chat_row(session, chat_id: int) -> Chat`,
  `def parse_args(argv: list[str]) -> argparse.Namespace`, `async def main(argv) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_import_history.py
from telegrind.import_history import parse_args


def test_import_takes_a_path_and_a_chat_id() -> None:
    args = parse_args(["import", "result.json", "--chat-id", "3260987"])
    assert args.command == "import"
    assert args.export == Path("result.json")
    assert args.chat_id == 3260987
    assert args.dry_run is False
    assert args.since is None


def test_since_parses_as_a_utc_date() -> None:
    args = parse_args(["import", "r.json", "--chat-id", "1", "--since", "2023-06-07"])
    assert args.since == datetime(2023, 6, 7, tzinfo=UTC)


def test_extract_takes_no_export() -> None:
    args = parse_args(["extract", "--chat-id", "3260987", "--batch", "10"])
    assert args.command == "extract"
    assert args.batch == 10
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_args'`

- [ ] **Step 3: Implement the CLI**

```python
# add to telegrind/import_history.py
import argparse
import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def chat_row(session: AsyncSession, chat_id: int) -> Chat:
    """The chat, created with the defaults if the import is its first sight.

    `tz_offset` 6 and `currency` KZT come from the column defaults; this
    import is one person's chat in Almaty and there is nothing else to
    read them from.
    """
    async with session.begin():
        found = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
        chat = found.scalar_one_or_none()
        if chat is None:
            chat = Chat(chat_id=chat_id)
            session.add(chat)
            await session.flush()
    return chat


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m telegrind.import_history")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="load a Telegram Desktop export")
    imp.add_argument("export", type=Path)
    imp.add_argument("--chat-id", type=int, required=True)
    imp.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        # UTC, not Almaty. The only intended use is cutting the first two
        # days of `asd`/`111` test junk, which are junk in any timezone; a
        # cutoff that had to land on a particular local midnight would need
        # the chat's tz_offset, which is not read here.
        help="skip entries older than this, UTC (the first two days are test junk)",
    )
    imp.add_argument("--dry-run", action="store_true")

    ext = sub.add_parser("extract", help="derive facts from what was imported")
    ext.add_argument("--chat-id", type=int, required=True)
    ext.add_argument("--batch", type=int, default=BATCH)
    ext.add_argument("--max-passes", type=int, default=None)

    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    # Same reason as main.py: the Chat row is read after its transaction
    # commits, and the default would expire it into implicit IO with no
    # greenlet on the stack.
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    # A session per phase, not one for the run. In SQLAlchemy 2 a bare read
    # autobegins, so a session that has read outside an explicit block makes
    # the next `session.begin()` raise «A transaction is already begun on
    # this Session» — and this repo's fake sessions yield from `begin()`
    # unconditionally, so no test here can catch it. A fresh session per
    # phase removes the question instead of answering it.
    async with async_session() as session:
        chat = await chat_row(session, args.chat_id)

    async with async_session() as session:
        chat = await session.merge(chat)
        if args.command == "import":
            export = read_export(args.export, chat_id=args.chat_id, since=args.since)
            report = await import_entries(
                session, chat, export, dry_run=args.dry_run
            )
            log.info(
                "seen=%s stored=%s skipped=%s verdicts=%s",
                report.seen,
                report.stored,
                report.skipped,
                dict(report.verdicts),
            )
        else:
            reports = await extract_all(
                session, chat, batch=args.batch, max_passes=args.max_passes
            )
            log.info(
                "passes=%s facts=%s complaints=%s",
                len(reports),
                sum(r.facts for r in reports),
                sum(r.complaints for r in reports),
            )

    await engine.dispose()


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    )
    asyncio.run(main())
```

Add `import sys` to the module's imports.

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_import_history.py -v`
Expected: PASS, 38 tests.

- [ ] **Step 5: Document it in CLAUDE.md**

Add to the Architecture section, after the `telegrind/coerce.py` paragraph:

```markdown
**`telegrind/import_history.py`** — the one-time v1 import, and the only
code here that writes messages without a Telegram update behind them. It
builds a real `aiogram.types.Message` per export entry and goes through
`store.upsert_message`, because three readers parse the `raw` JSONB
afterwards and a differently-shaped dump inserts fine before
misattributing every imported line. `verdict` is asserted from the
entry's structure — the bot's own messages, the slash commands, the 56
bare `-` markers — and passed explicitly at every call, never classified:
3915 model calls would buy a worse answer than the export already
contains. Extraction is a separate subcommand driving `extract.run` at
twenty messages a pass, because `_pass` makes one model call for the
whole tail against a 2048-token reply. Design:
`docs/superpowers/specs/2026-09-16-history-import-design.md`.
```

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format telegrind/import_history.py tests/test_import_history.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/import_history.py tests/test_import_history.py CLAUDE.md
git commit -m "feat: the import and extract subcommands"
```

---

### Task 7: The dev-stack walkthrough — a gate, not a milestone

**Files:** none. This task produces numbers, and the numbers decide whether
Task 9 happens at all.

CLAUDE.md's rule applies literally here: every end-to-end walk on this repo has
found a defect the unit suite structurally could not see. Do not skip it.

- [ ] **Step 1: Bring up the dev database**

```bash
./dev-setup.sh          # this worktree has no .env yet; it writes one
docker compose up -d postgres
uv run alembic upgrade head
docker compose exec postgres psql -U postgres -l
```

The database is **`postgres`**, not `telegrind` — `compose.yml` points the bot at
`postgresql+asyncpg://postgres:…@postgres/postgres`, and both prod stacks answer
on the same name. The `-l` listing is there so nobody discovers this at Step 4.

- [ ] **Step 2: Dry-run the import**

```bash
uv run python -m telegrind.import_history import \
  "/home/me/Загрузки/Telegram Desktop/ChatExport_2026-09-16/result.json" \
  --chat-id 3260987 --dry-run
```

Expected, from the 2026-09-16 measurement: `seen=6835`, `stored=0`,
`skipped=0`, and verdicts totalling exactly 6835 — `fact` 3986 (3915 with
readable text plus the 71 photos and stickers, which are `fact` and simply
never reach the tail), `system` 2849 (2766 the bot's + 27 `/start` + 56
dashes), `question` 0. The two must sum to 6835; if they do not, the export
changed — stop and re-measure rather than adjusting the expectation.

- [ ] **Step 3: Import for real**

```bash
uv run python -m telegrind.import_history import \
  "/home/me/Загрузки/Telegram Desktop/ChatExport_2026-09-16/result.json" \
  --chat-id 3260987
```

- [ ] **Step 4: Check what landed, in SQL**

```bash
docker compose exec postgres psql -U postgres -d postgres \
  -c "select verdict, count(*) from message group by verdict order by 2 desc" \
  -c "select count(*) from message where verdict = 'fact' and extracted_at is null and coalesce(nullif(trim(text), ''), transcript) is not null" \
  -c "select message_id, tg_date, text from message order by tg_date desc limit 5" \
  -c "select count(*) from message where raw->'from_user'->>'is_bot' = 'true'"
```

Watch for `A transaction is already begun on this Session` in Step 3 and Step 6.
If it appears, a bare read escaped its `session.begin()` — the fix is a fresh
session, never a nested `begin()`.

Expected: the verdict counts from Step 2; the pending-tail count 3915 (the 3986
`fact` rows minus the 71 with nothing readable); the last five rows
matching the tail of the export (`449 usd apple watch series 12 45mm gold`
last); and 2766 bot rows.

- [ ] **Step 5: Re-run the import and prove it is idempotent**

Run Step 3 again, then re-run the first query of Step 4. Expected: identical
counts, no duplicate-key error. A second copy of every row means
`upsert_message` was not reached and the run wrote directly.

- [ ] **Step 6: Extract three passes and read them**

```bash
uv run python -m telegrind.import_history extract --chat-id 3260987 --max-passes 3
docker compose exec postgres psql -U postgres -d postgres \
  -c "select kind, count(*) from fact where deleted_at is null group by kind order by 2 desc" \
  -c "select f.kind, f.at, f.fields, m.text from fact f join message m on m.id = f.message_pk order by f.id limit 20"
```

Read the twenty rows against their texts. What matters is not that they parsed
but that the `kind` vocabulary is the chat's own — `expense`, `wish`, `loan`,
`health` — and that measurements did not land in the `facts` catch-all, which is
the defect the `-m llm` gate went 24/24 green through.

- [ ] **Step 7: Record the numbers**

Write down: cost of three passes (the Anthropic console), wall time per pass, the
observed `kind` distribution, and the complaint count. These are what step 4 of
the spec's order of work asks for, and Task 9 is refused without them.

- [ ] **Step 8: Finish the corpus**

```bash
uv run python -m telegrind.import_history extract --chat-id 3260987
```

Expect roughly 200 passes. If a pass fails, the loop stops by design — read
`message.extract_error`, fix, and re-run; the tail is still pending, so nothing
is lost.

---

### Task 8: The workbook comparison

**Files:**
- Create: `telegrind/workbook_compare.py`
- Test: `tests/test_workbook_compare.py`

**Prerequisite:** the three worksheets (`Outcome`, `Loan`, `Wish`) exported to
CSV by hand, one file each. **gspread does not come back** — it was deleted in
`248fe9d` and a one-time report is not a reason to restore it.

The comparison reads the sheets generically: column A is the key, every other
column is carried through under its header. Nothing here interprets the old
column names, because the report is for a human to read, and guessing a mapping
from old columns to new `fields` is exactly the kind of assumption the report
exists to test.

**Interfaces:**
- Produces: `def message_id_of(key: str) -> int | None`,
  `def read_sheet(path: Path) -> tuple[dict[str, dict], Counter]`,
  `async def report(session, chat_pk: int, sheets: dict[str, dict], out: Path) -> Counter`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workbook_compare.py
from pathlib import Path

from telegrind.workbook_compare import message_id_of, read_sheet


def test_a_bare_id_is_the_pre_9540232_form() -> None:
    assert message_id_of("641715") == 641715


def test_an_id_with_a_seq_is_the_later_form() -> None:
    assert message_id_of("641715_2") == 641715


def test_a_key_that_is_not_an_id_is_none() -> None:
    assert message_id_of("") is None
    assert message_id_of("итого") is None


def test_a_repeated_key_drops_both_and_is_counted(tmp_path: Path) -> None:
    """A duplicate is a connectivity retry, and a labelled set whose
    labels are guesses is worth nothing — drop and count, never repair."""
    path = tmp_path / "Outcome.csv"
    path.write_text("key,amount,category\n641715,449,tech\n641715,500,tech\n", encoding="utf-8")
    rows, dropped = read_sheet(path)
    assert rows == {}
    assert dropped["duplicate"] == 2


def test_distinct_keys_survive(tmp_path: Path) -> None:
    path = tmp_path / "Outcome.csv"
    path.write_text("key,amount\n641715_1,449\n641715_2,500\n", encoding="utf-8")
    rows, dropped = read_sheet(path)
    assert sorted(rows) == ["641715_1", "641715_2"]
    assert rows["641715_1"]["amount"] == "449"
    assert not dropped
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_workbook_compare.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement the reader**

```python
# telegrind/workbook_compare.py
"""The one-time comparison of v1's workbook against v2's facts.

The workbook never stored the text, so it cannot be an import source. It
stored what the OLD extractor made of each message, joined to the export
by message id at no cost — including every row the user corrected by
hand. That makes it a labelled set, and the only evidence available that
the export's ids are the ids v1 recorded.

Reads CSV files. gspread is not coming back.
"""

import csv
import json
import logging
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import Fact, LoggedMessage

log = logging.getLogger(__name__)


def message_id_of(key: str) -> int | None:
    """Column A: a bare `message_id`, or `<message_id>_<seq>` after 9540232."""
    head = key.strip().split("_", 1)[0]
    return int(head) if head.isdigit() else None


def read_sheet(path: Path) -> tuple[dict[str, dict], Counter]:
    """Rows by their full key, with ambiguous ones dropped and counted.

    Counted in one pass over the keys first, so that a key appearing
    three times drops all three rather than «the first one wins».
    """
    dropped: Counter = Counter()
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    keyed: list[tuple[str, dict]] = []
    for row in rows:
        key = str(next(iter(row.values())) or "").strip()
        if message_id_of(key) is None:
            dropped["unparsable"] += 1
            continue
        keyed.append((key, row))

    counts = Counter(key for key, _ in keyed)
    dropped["duplicate"] = sum(n for n in counts.values() if n > 1)
    return {key: row for key, row in keyed if counts[key] == 1}, dropped
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_workbook_compare.py -v`
Expected: PASS, 5 tests.

A key appearing three times must drop all three, not keep the first. Add that
case if the implementation tempts you otherwise.

- [ ] **Step 5: Add the report**

```python
# add to telegrind/workbook_compare.py
async def report(
    session: AsyncSession, chat_pk: int, sheets: dict[str, dict], out: Path
) -> Counter:
    """Write old rows beside new facts, message by message.

    Three outcomes per workbook row, and the second is the interesting
    one: matched, or the message is missing from the log — which means it
    was deleted from the chat and the workbook is its only remaining
    trace — or the message is there and produced no fact.
    """
    tally: Counter = Counter()
    lines: list[str] = []

    messages = {
        row.message_id: row
        for row in (
            await session.execute(
                select(LoggedMessage).where(LoggedMessage.chat_pk == chat_pk)
            )
        ).scalars()
    }
    facts: dict[int, list[Fact]] = {}
    for fact in (
        await session.execute(
            select(Fact).where(Fact.chat_pk == chat_pk, Fact.deleted_at.is_(None))
        )
    ).scalars():
        facts.setdefault(fact.message_pk, []).append(fact)

    for sheet, rows in sheets.items():
        for key, old in sorted(rows.items()):
            message_id = message_id_of(key)
            row = messages.get(message_id) if message_id is not None else None
            if row is None:
                tally["missing_message"] += 1
                lines.append(f"{sheet}\t{key}\tMISSING\t{json.dumps(old, ensure_ascii=False)}")
                continue
            new = facts.get(row.id, [])
            tally["matched" if new else "no_fact"] += 1
            lines.append(
                f"{sheet}\t{key}\t{row.text!r}\t"
                f"{json.dumps(old, ensure_ascii=False)}\t"
                f"{json.dumps([{'kind': f.kind, **f.fields} for f in new], ensure_ascii=False)}"
            )

    out.write_text("\n".join(lines), encoding="utf-8")
    return tally
```

Wire it behind a `__main__` block taking the CSV paths, `--chat-id` and `--out`,
in the shape Task 6 used.

- [ ] **Step 6: Run it and read the tally**

Expected shape of the answer, from the spec: matched rows are the labelled set;
`missing_message` rows are messages deleted from the chat; `no_fact` rows are
where the new extractor found nothing the old one did. **`missing_message` is
also the id check** — a large count means the export's ids are not v1's ids, and
that invalidates the whole join rather than the rows.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff format telegrind/workbook_compare.py tests/test_workbook_compare.py
uv run ruff check && uv run ty check && uv run pytest
git add telegrind/workbook_compare.py tests/test_workbook_compare.py
git commit -m "feat: compare v1's workbook against what v2 extracted"
```

---

### Task 9: Production — a gate, not a milestone

Refused until Tasks 7 and 8 have produced their numbers and the old-facts
question (spec §Scope) has been decided.

- [ ] **Step 1: Copy the export to latitude**

```bash
scp "/home/me/Загрузки/Telegram Desktop/ChatExport_2026-09-16/result.json" \
  latitude:/tmp/result.json
```

- [ ] **Step 2: Deploy the branch to the v2 stack**

On latitude, in the v2 checkout: `git pull`, then
`docker compose -f compose.prod.yml up -d --build`. The image is the rollback
unit; record the previous image id before building.

- [ ] **Step 3: Dry-run inside the stack**

```bash
docker compose -p telegrind-v2 -f compose.prod.yml run --rm \
  -v /tmp/result.json:/import/result.json:ro bot \
  python -m telegrind.import_history import /import/result.json --chat-id 3260987 --dry-run
```

- [ ] **Step 4: Import, then extract**

Same command without `--dry-run`, then the `extract` subcommand. Watch the first
three passes before leaving it to run.

- [ ] **Step 5: Ask the bot something**

Send a real question into the v2 chat — «сколько я потратил на кофе в августе» —
and check the answer against the workbook. This is the only check that the
import, the extraction and the query path agree, and no test reaches it.

---

## Self-review

**Spec coverage.** Export measurement → Task 3 Step 5 and Task 7 Step 2.
`aiogram`-message construction and the `raw` argument → Task 2. The field
mapping table, including `edit_date` and the voice placeholder → Task 2.
Verdict assertion without the classifier → Tasks 1 and 4. `verdict` passed at
every call site → Task 4, with a discrimination step that reproduces the
`db9de98` trap. Not going through the dispatcher → Task 4's docstring and the
absence of any handler import. `--chat-id` as authority, bot id derived, third
author refused → Task 3. `--since` → Tasks 3 and 6. Batch size bounded by
`MAX_TOKENS` → Task 5, asserted as a test. Dev before prod → Tasks 7 and 9.
Workbook join, drop-don't-repair, deleted messages, the id check → Task 8.
`PROMPT_VERSION` untouched → Global Constraints; no task edits `llm.py`.

**Not covered, deliberately:** whether the workbook's facts are written into
`fact`. That is the spec's step 6 and it is gated on Task 8's tally. If the
answer turns out to be yes, it is a new plan — it needs a `seq` scheme that
survives re-extraction, and `replace_facts` diffs by `seq`.

**Type consistency.** `verdict_of(entry, *, bot_id)` is called with a keyword in
every task. `message_from(entry, *, chat_id, bot_id)` likewise. `Export` carries
`bot_id` and `entries`, and Task 4 reads both. `extract_all` takes `run`, Task 4
takes `upsert` — both named the way `extract.run` names `call`. `ImportReport`
fields (`seen`, `stored`, `skipped`, `verdicts`) are the ones Task 6 logs.
