"""The user's reaction is the delete gesture.

The bot has already put 💔 on the message, so the user taps that existing
bubble — one tap, no picker. Any reaction deletes, because the Bot API
cannot narrow which emoji a private chat offers: setMessageReaction sets
the *bot's* reaction and available_reactions is read-only. Since the
picker cannot be narrowed, the accepted set is widened instead.

Measured 2026-09-11: message_reaction does reach a bot in a private chat
despite the docs' "must be an administrator", the bot's own reaction
generates no update, and old_reaction/new_reaction carry this one user's
reactions rather than the message total — so nothing here has to work out
whose reaction it is looking at.
"""

import logging

from aiogram.types import MessageReactionUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import store
from telegrind.bot.router import router
from telegrind.models import Chat

log = logging.getLogger(__name__)


def wants_delete(event: MessageReactionUpdated) -> bool:
    """True when the user now has a reaction on the message."""
    return bool(event.new_reaction)


@router.message_reaction()
async def toggle_delete(
    message_reaction: MessageReactionUpdated, chat: Chat, session: AsyncSession
) -> None:
    """Tombstone the message's facts, or lift the tombstone.

    There is no reply and no counter-reaction: the bubble the user just
    tapped is already the visible state.
    """
    async with session.begin():
        row = await store.get_message(session, chat.id, message_reaction.message_id)
        if row is None:
            log.info("reaction on unknown message %s", message_reaction.message_id)
            return
        if wants_delete(message_reaction):
            count = await store.tombstone_facts(session, row.id, message_reaction.date)
            log.info(
                "tombstoned %s fact(s) of message %s",
                count,
                message_reaction.message_id,
            )
        else:
            count = await store.restore_facts(session, row.id)
            log.info(
                "restored %s fact(s) of message %s", count, message_reaction.message_id
            )
