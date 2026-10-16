"""ElevenLabs Scribe: speech to raw text, primed with the person's terms (SPEC REQ-VOICE-2).

The audio is uploaded, never linked: the Telegram file URL carries the bot token."""

from __future__ import annotations

import httpx

URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v2"
TIMEOUT_S = 180


class ScribeError(Exception):
    pass


async def transcribe(client: httpx.AsyncClient, api_key: str, data: bytes, filename: str, keyterms: list[str]) -> str:
    # Language is auto-detected: forcing ru turns English terms into Cyrillic
    form: dict[str, str | list[str]] = {"model_id": MODEL, "tag_audio_events": "false"}
    if keyterms:
        form["keyterms"] = keyterms
    resp = await client.post(
        URL, headers={"xi-api-key": api_key}, data=form, files={"file": (filename, data)}, timeout=TIMEOUT_S
    )
    if resp.status_code != 200:
        raise ScribeError(f"HTTP {resp.status_code}")
    return (resp.json().get("text") or "").strip()
