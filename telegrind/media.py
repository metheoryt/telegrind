"""Photos and documents: downloaded into media/<sha256> after the commit, with retries (SPEC REQ-MSG-2)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from io import BytesIO
from pathlib import Path
from typing import Any

from telegrind.app import App
from telegrind.incoming import Incoming

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024  # the Bot API's getFile cap
MAX_ATTEMPTS = 5
BACKOFF_S = (1, 2, 4, 8, 16)


def store_bytes(media_dir: Path, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    target = media_dir / sha
    if target.exists():
        return sha
    tmp = media_dir / f".{sha}.part-{os.getpid()}"
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return sha


async def download(bot: Any, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


class MediaJobs:
    def __init__(self, app: App, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.app, self.sleep = app, sleep

    def enqueue(self, uid: int, inc: Incoming) -> None:
        assert inc.media is not None
        store = self.app.log_for(uid)
        store.media_enqueue(inc.message_id, inc.media.file_id, inc.media.name, inc.media.mime, inc.media.size)
        if inc.media.size is not None and inc.media.size > MAX_BYTES:
            store.media_set(inc.message_id, "too_large", error=f"{inc.media.size} bytes")
            return
        self.app.spawn(self.run(uid, inc.message_id))

    def resume(self) -> None:
        for uid in self.app.layout.users():
            for row in self.app.log_for(uid).media_pending():
                self.app.spawn(self.run(uid, row["message_id"]))

    async def run(self, uid: int, message_id: int) -> None:
        store = self.app.log_for(uid)
        row = store.conn.execute(
            "SELECT file_id, name, mime, attempts FROM media WHERE message_id = ? AND status = 'pending'", (message_id,)
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"])
        while attempts < MAX_ATTEMPTS:
            try:
                data = await download(self.app.bot, row["file_id"])
            # Any failure of the Bot API or the network; the message may carry the token, so log the type only.
            except Exception as exc:
                attempts += 1
                store.media_set(
                    message_id,
                    "pending" if attempts < MAX_ATTEMPTS else "failed",
                    error=type(exc).__name__,
                    attempt=True,
                )
                if attempts >= MAX_ATTEMPTS:
                    log.warning(
                        "user %d: media %d failed after %d attempts: %s", uid, message_id, attempts, type(exc).__name__
                    )
                    return
                await self.sleep(BACKOFF_S[attempts - 1])
                continue
            sha = store_bytes(self.app.layout.media_dir(uid), data)
            store.record_stored(message_id, sha, row["name"], row["mime"], len(data), self.app.clock())
            store.media_set(message_id, "stored", sha256=sha)
            return
