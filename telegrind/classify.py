"""What a message is, and therefore what happens to it.

One call, and it happens *after* the row is committed and *outside* any
transaction: nothing written is ever lost, and that invariant must not come
to depend on a model call succeeding — or returning. `record` therefore
commits the row as a `fact` first and refines it in a second transaction
once this answers; see bot/handlers/handlers.py. A failure here defaults to
`fact` for the same reason — 💔 goes on, the message enters the tail, and
the behaviour is exactly what shipped before the classifier existed.

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
    continues: str | None,
    call: Callable[..., Awaitable[dict]] = llm.use_tool,
) -> str:
    """One classification. Never raises.

    `continues` is the verdict of the turn this message replies into, or
    None when it starts one. It has no default on purpose: a permissive
    default on a new routing input is exactly how `verdict` itself once
    re-admitted every slash command to the extraction tail, and nothing
    failed — the rows were simply parsed.
    """
    decided = presumed(text)
    if decided is not None:
        return decided

    # The reply chain outranks the model, and it is read *after*
    # `presumed`: that order is chosen, not inherited. /q typed as a reply
    # to Claude still means «ask the ledger», and a sticker sent into a
    # conversation still has nothing readable to hand over. What the chain
    # decides is the case the text cannot: «а последний коммит какой» is a
    # question about the ledger on its own and a continuation in a thread,
    # and only one of the two is true at a time.
    if continues == VERDICT_TALK:
        return VERDICT_TALK

    try:
        payload = await call(llm.CLASSIFY_SYSTEM, text or "", llm.CLASSIFY_TOOL)
        verdict = str(payload.get("verdict") or "")
    except Exception as exc:
        log.warning("classifier failed, defaulting to a fact: %s", exc)
        return VERDICT_FACT

    if verdict not in _ASKED:
        log.warning("classifier returned %r, defaulting to a fact", verdict)
        return VERDICT_FACT
    return verdict
