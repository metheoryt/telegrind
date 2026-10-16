"""Media downloads into media/<sha256> with retries (SPEC REQ-MSG-2)."""

import hashlib

import pytest
from tgkit import FakeBot, user_message

from telegrind.app import App
from telegrind.config import Config
from telegrind.layout import Layout
from telegrind.media import MAX_BYTES, MediaJobs, store_bytes


@pytest.fixture
def app(tmp_path):
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: 1_760_000_000)
    slept = []

    async def sleep(s):
        slept.append(s)

    app.media = MediaJobs(app, sleep=sleep)
    app.slept = slept
    yield app
    app.close()


def test_store_bytes_is_content_addressed_and_idempotent(tmp_path):
    """REQ-MSG-2: one file per content; a second store of the same bytes changes nothing."""
    sha = store_bytes(tmp_path, b"hello")
    assert sha == hashlib.sha256(b"hello").hexdigest() and (tmp_path / sha).read_bytes() == b"hello"
    assert store_bytes(tmp_path, b"hello") == sha and len(list(tmp_path.iterdir())) == 1


async def test_photo_is_downloaded_after_the_receipt(app):
    """REQ-MSG-2: stored first with its heart, then downloaded; a `stored` event sets media_sha256."""
    app.bot.files["p-big"] = b"JPEGDATA"
    await app.on_message(user_message(photo=True))
    await app.drain()
    entry = app.log_for(7).entry(10)
    sha = hashlib.sha256(b"JPEGDATA").hexdigest()
    assert entry.media_sha256 == sha and entry.media_name == "photo.jpg" and entry.revision == 2
    assert (app.layout.media_dir(7) / sha).read_bytes() == b"JPEGDATA"
    assert app.bot.names().index("set_message_reaction") < app.bot.names().index("get_file")
    assert app.log_for(7).media_pending() == []


async def test_download_retries_with_backoff_then_fails(app):
    """REQ-MSG-2: up to 5 attempts with backoff, then `failed`; the entry stays."""
    app.bot.fail["get_file"] = 99
    await app.on_message(user_message(document=("d9", "kaspi.pdf", 30_000)))
    await app.drain()
    row = app.log_for(7).conn.execute("SELECT status, attempts, last_error FROM media WHERE message_id = 10").fetchone()
    assert (row["status"], row["attempts"]) == ("failed", 5) and row["last_error"]
    assert app.slept == [1, 2, 4, 8] and app.log_for(7).entry(10).media_sha256 is None


async def test_too_large_is_not_fetched(app):
    """REQ-MSG-2: over the Bot API's 20 MB, marked too_large and never requested."""
    await app.on_message(user_message(document=("d9", "big.bin", MAX_BYTES + 1)))
    await app.drain()
    assert app.log_for(7).conn.execute("SELECT status FROM media WHERE message_id = 10").fetchone()[0] == "too_large"
    assert "get_file" not in app.bot.names()


async def test_resume_picks_up_pending_rows(app):
    """REQ-MSG-2: a download interrupted by a restart is retried from the `media` table."""
    log = app.log_for(7)
    from telegrind.incoming import incoming

    log.record_received(incoming(user_message(photo=True), now=1))
    log.media_enqueue(10, "p-big", "photo.jpg", "image/jpeg", 100)
    app.bot.files["p-big"] = b"X"
    app.media.resume()
    await app.drain()
    assert log.entry(10).media_sha256 == hashlib.sha256(b"X").hexdigest()
