"""A session is a reply chain, and it is never walked.

One hop off `reply_to_message`, at most one more off our own stored row for
the parent. The chain lives in the transcripts; the database only ever
answers «what is the parent». Nothing is stored to make this work — the id
is derived, so the session graph cannot drift from what Telegram shows the
user.

Reply means two different things and they separate mechanically: a reply to
one's own message is fact chaining, a reply to a Claude message continues the
conversation. Because a recorded fact gets no text reply, there is nothing of
Claude's to reply to on the recording path, and the two cannot collide.
"""

import uuid
from collections.abc import Awaitable, Callable

from aiogram.types import Message

#: A fixed namespace so the derivation is reproducible across restarts and
#: across machines. Generated once with uuid4 and frozen.
NAMESPACE = uuid.UUID("7a9f2d1e-5c34-4b8a-9e61-0d3f8c2a7b45")


def session_id(chat_id: int, message_id: int) -> uuid.UUID:
    """The session a turn writes to: the user message that caused it."""
    return uuid.uuid5(NAMESPACE, f"{chat_id}:{message_id}")


async def turn_key(
    message: Message,
    *,
    parent_of: Callable[[int], Awaitable[int | None]],
) -> int:
    """Which turn this message continues, as a Telegram message_id.

    Two hops at most. The parent is the user's own message → that is the
    turn. The parent is a bot message → the turn is the message *it*
    replied to, which exists because the bot always sends with reply
    parameters. Both land on the same place, which is the point: replying
    to one's own question and replying to the answer are the same place in
    the conversation, and the user should not have to know which continues
    it.

    Telegram does not nest replies — `reply_to_message.reply_to_message` is
    always None — so the second hop is `parent_of`, a lookup in our own
    rows.
    """
    parent = message.reply_to_message
    if parent is None:
        return message.message_id

    sender = getattr(parent, "from_user", None)
    if sender is not None and sender.is_bot:
        grandparent = await parent_of(parent.message_id)
        return grandparent if grandparent is not None else parent.message_id

    return parent.message_id
