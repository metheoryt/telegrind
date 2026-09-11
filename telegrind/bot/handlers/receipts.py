"""The receipt reaction: the emoji, the cycle, and placing it.

Split out of `handlers.py` because it registers nothing. Importing a
handler module is what registers its handlers, so anything that needs
`RECEIPT_EMOJI` — `query.py` does — would otherwise pull the whole
catch-all module in ahead of itself and lose the registration race.
"""

import logging

from aiogram import Bot
from aiogram.types import ReactionTypeEmoji

log = logging.getLogger(__name__)

#: What the bot puts on a stored message, and what an edit advances it to.
#: The bubble's presence is the receipt; its emoji names what tapping it
#: does, because tapping it is the delete gesture. A broken heart warns
#: without 👎's flavour of the bot disapproving of every line the user
#: writes, and the two it cycles through stay in the same family so the
#: warning survives the cycle. All three verified against
#: setMessageReaction on 2026-09-11.
RECEIPT_CYCLE = ("💔", "❤‍🔥", "💘")

#: What a message gets the first time it is stored.
RECEIPT_EMOJI = RECEIPT_CYCLE[0]


def next_receipt(current: str | None) -> str:
    """The emoji an edit moves the receipt to.

    `None` means a row stored before the column existed. Those visibly
    carry the default, because that is all the old code ever placed, so
    they advance off it rather than re-place it. Anything unrecognised
    also advances: the whole point is that an edit looks different.
    """
    try:
        index = RECEIPT_CYCLE.index(current or RECEIPT_EMOJI)
    except ValueError:
        index = 0
    return RECEIPT_CYCLE[(index + 1) % len(RECEIPT_CYCLE)]


async def acknowledge(bot: Bot, chat_id: int, message_id: int, emoji: str) -> None:
    """Place the receipt reaction. Never fatal.

    The message row is committed before this runs, so a Telegram failure
    here costs a visual cue and nothing else. Raising would lose the
    update; the invariant is about the row, not the bubble. The row then
    names an emoji the message does not show, which the next edit
    corrects by advancing again.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:  # cosmetic, and the row is already safe
        log.warning("could not set the receipt reaction on %s", message_id)
