"""Drive `/start` directly.

A crash inside a handler body answers nothing at all, which from the chat
looks exactly like a bot that is down — so the one handler with no early
return gets exercised here rather than by hand. Fakes stand in for the
session and for aiogram's `Message`; the assertions are about what the user
receives and what gets cached.
"""

from types import SimpleNamespace

from telegrind.bot.const import INTRO_TEXT, TIP_TEXT
from telegrind.bot.handlers.start import INTRO_FILENAME, start
from telegrind.models import File


class FakeResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class FakeSession:
    """Just enough AsyncSession for `start`."""

    def __init__(self, row: object | None = None) -> None:
        self.row = row
        self.added: list[object] = []

    def begin(self) -> FakeSession:
        return self

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, _statement: object) -> FakeResult:
        return FakeResult(self.row)

    def add(self, obj: object) -> None:
        self.added.append(obj)


class FakeSent:
    def __init__(self, file_id: str | None) -> None:
        self.video = SimpleNamespace(file_id=file_id) if file_id else None
        self.pinned = False

    async def pin(self, disable_notification: bool = False) -> None:
        self.pinned = True


class FakeMessage:
    def __init__(self) -> None:
        self.videos: list[object] = []
        self.captions: list[str] = []
        self.texts: list[str] = []
        self.sent: list[FakeSent] = []

    async def answer_video(self, video: object, caption: str) -> FakeSent:
        self.videos.append(video)
        self.captions.append(caption)
        sent = FakeSent("BAACAgIAAx0")
        self.sent.append(sent)
        return sent

    async def answer(self, text: str) -> FakeSent:
        self.texts.append(text)
        sent = FakeSent(None)
        self.sent.append(sent)
        return sent


async def test_start_uploads_the_video_and_caches_its_file_id() -> None:
    message = FakeMessage()
    session = FakeSession(row=None)

    await start(message, session)  # type: ignore[arg-type]

    assert message.captions == [INTRO_TEXT]
    assert message.texts == [TIP_TEXT]
    assert len(session.added) == 1
    cached = session.added[0]
    assert isinstance(cached, File)
    assert cached.filename == INTRO_FILENAME
    assert cached.file_id == "BAACAgIAAx0"


async def test_start_reuses_the_cached_file_id_and_caches_nothing_new() -> None:
    """The second /start is the one that exercises the cache hit."""
    message = FakeMessage()
    session = FakeSession(row=File(file_id="CACHED", filename=INTRO_FILENAME))

    await start(message, session)  # type: ignore[arg-type]

    assert message.videos == ["CACHED"]
    assert session.added == []


async def test_start_pins_the_tips_and_not_the_video() -> None:
    """The tips are the message worth having pinned in the chat."""
    message = FakeMessage()
    await start(message, FakeSession(row=None))  # type: ignore[arg-type]

    video_msg, tips_msg = message.sent
    assert not video_msg.pinned
    assert tips_msg.pinned


async def test_start_asks_for_nothing() -> None:
    """Onboarding is gone: /start must not request a spreadsheet URL.

    The old flow put the chat into an FSM state here and refused to record
    anything until a link arrived, which made the bot's first answer a
    refusal. The copy is the only remaining place that could reintroduce it.
    """
    message = FakeMessage()
    await start(message, FakeSession(row=None))  # type: ignore[arg-type]

    said = " ".join(message.captions + message.texts)
    assert "пришлите ссылку" not in said.lower()
    assert "/link" in said
