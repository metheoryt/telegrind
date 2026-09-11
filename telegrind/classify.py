"""What a message is, and therefore what happens to it.

One call, and it happens *after* the row is committed: nothing written is
ever lost, and that invariant must not come to depend on a model call
succeeding. A failure therefore defaults to `fact` — 💔 goes on, the
message enters the tail, and the behaviour is exactly what shipped before
the classifier existed.

This is the only place the verdict is decided. The slash rule lives here
rather than in an aiogram filter because two rules that can disagree is a
bug found in production, not a design.
"""

import logging
from collections.abc import Awaitable, Callable

from telegrind import llm
from telegrind.models import (
    VERDICT_FACT,
    VERDICT_QUESTION,
    VERDICT_SYSTEM,
    VERDICT_TALK,
)

log = logging.getLogger(__name__)

_ASKED = (VERDICT_FACT, VERDICT_QUESTION, VERDICT_TALK)


def presumed(text: str | None) -> str | None:
    """The verdict that needs no model call, or None if one is needed.

    Three cases. `/q` is the explicit override and sets the verdict rather
    than bypassing it. Any other slash command is `system` — a command must
    never coin a kind. And a message with nothing readable (a sticker, a
    photo, a voice note before ASR) is a `fact` that `_has_content()`
    already keeps out of the tail, so the call would buy nothing.
    """
    body = (text or "").strip()
    if not body:
        return VERDICT_FACT
    if body.startswith("/"):
        command = body.split(maxsplit=1)[0].split("@")[0]
        return VERDICT_QUESTION if command == "/q" else VERDICT_SYSTEM
    return None


async def verdict_for(
    text: str | None,
    *,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> str:
    """One classification. Never raises."""
    decided = presumed(text)
    if decided is not None:
        return decided

    try:
        payload = await call(llm.CLASSIFY_SYSTEM, text or "", llm.CLASSIFY_TOOL)
    except Exception as exc:
        log.warning("classifier failed, defaulting to a fact: %s", exc)
        return VERDICT_FACT

    verdict = str(payload.get("verdict") or "")
    if verdict not in _ASKED:
        log.warning("classifier returned %r, defaulting to a fact", verdict)
        return VERDICT_FACT
    return verdict
