"""Ingestion. Store, commit, classify, and act on the verdict.

There is no echo on the fact path: the confirmation that the bot understood
is 💔, and that reaction is also the delete affordance. What the classifier
decides is what happens next — see bot/routing.py.

The row is committed *before* the classifier runs — two transactions, with
the model call between them and inside neither. Nothing written is ever
lost, and that must not come to depend on a model call: aiogram advances
the polling offset as it dispatches, so an update lost mid-handler is never
redelivered. Do not fold the two back into one. `record_edited` honours the
same rule in the mirror order — read, classify, then write, since an edit
has a previous row to consult — so neither handler ever holds a write
transaction open across a model call.

There is no COMMAND_LIKE filter any more. The slash rule lives in
`classify.presumed`, because two rules that can disagree about whether a
message is a command is a bug found in production.
"""

import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import classify, extract, store
from telegrind.bot.handlers.receipts import (
    HANDED_OVER,
    RECEIPT_EMOJI,
    clear_receipt,
    next_receipt,
)
from telegrind.bot.handlers.receipts import (
    RECEIPT_CYCLE as RECEIPT_CYCLE,
)
from telegrind.bot.router import router
from telegrind.bot.routing import HandOver, route
from telegrind.config import ChatConfig
from telegrind.models import VERDICT_FACT, Chat

log = logging.getLogger(__name__)

#: Set by setup_dispatcher() when the meta layer is configured. None means
#: there is nobody to hand a message to, and routing says so rather than
#: going silent.
HAND_OVER: HandOver | None = None


async def _continues(
    session: AsyncSession, chat_pk: int, message: Message
) -> str | None:
    """The verdict of the turn this message replies into, or None.

    Read *after* the row is committed, never before: a lookup that fails
    ahead of the commit loses the update, and this one is a refinement of
    the routing decision, not part of storing anything.

    In a transaction of its own, because a bare read autobegins one that
    never closes and the next `session.begin()` then raises «a transaction
    is already begun».
    """
    parent = message.reply_to_message
    if parent is None:
        return None
    async with session.begin():
        root_id = await store.turn_root(session, chat_pk, parent.message_id)
        root = await store.get_message(session, chat_pk, root_id)
        return root.verdict if root is not None else None


@router.message()
async def record(
    message: Message, chat: Chat, config: ChatConfig, session: AsyncSession, bot: Bot
) -> None:
    """Store anything the user sent, then route it.

    No filter, deliberately: a sticker, a photo or a document is a message
    the user sent, so it is stored. `message_values` already handles a
    caption and a missing text, and `classify.presumed` gives a message
    with nothing readable a `fact` verdict without spending a call.

    Written first, classified second. aiogram advances the polling offset
    as it dispatches and runs handlers fire-and-forget, so a restart or a
    hung Anthropic call inside this function loses an update that is never
    redelivered — and the classifier is the only part of it that can hang.
    """
    # The first sighting is a fact, which is what shipped before the
    # classifier existed. Committing that is the invariant; everything
    # after it is refinement.
    async with session.begin():
        row, _ = await store.upsert_message(
            session, chat, message, verdict=VERDICT_FACT
        )
        row.receipt_emoji = RECEIPT_EMOJI

    verdict = await classify.verdict_for(
        message.text or message.caption,
        continues=await _continues(session, chat.id, message),
    )

    # A second, short transaction, and the model call is outside both. Die
    # between them and the row is a stored fact in the extraction tail —
    # the pre-classifier behaviour, which is the correct way to degrade.
    if verdict != VERDICT_FACT:
        async with session.begin():
            row.verdict = verdict
            # No receipt is placed until `route` runs, so clearing the
            # column here is not a promise being withdrawn — it is the
            # column catching up with a verdict that earns no receipt.
            row.receipt_emoji = None

    await route(message, row, chat, config, session, bot, hand_over=HAND_OVER)


@router.edited_message()
async def record_edited(
    edited_message: Message,
    chat: Chat,
    session: AsyncSession,
    config: ChatConfig,
    bot: Bot,
) -> None:
    """Overwrite the text, re-derive the verdict, and act on it.

    An edit can change the answer: «взял 3000» corrected to «потратил 3000
    на такси» is exactly the case where a fact moves from one kind to
    another, and correcting data or a typo is what edits are actually used
    for. So the verdict is re-derived on the same principle as extracted_at.

    👀 is the point of no return. A message already handed to Claude is
    still overwritten — nothing written is ever lost — but nothing
    downstream reacts to it: no re-send, no new turn, and no
    re-classification either. The correct behaviour would be to rewind the
    session, which is transcript surgery on a .jsonl we do not own, for a
    gesture whose workaround is saying the correction out loud.

    The extracted-or-not check has to happen *before* upsert_message, which
    clears extracted_at by design — and inside a transaction of its own, or
    the next `session.begin()` raises on the autobegun one.
    """
    # Three blocks, and the boundaries are load-bearing. A bare read
    # autobegins, so `previous` cannot be fetched outside a transaction or
    # the `session.begin()` below raises «a transaction is already begun» —
    # the same trap `routing.py`'s question arm documents. And the classifier
    # call must sit between two transactions, never inside one.
    async with session.begin():
        previous = await store.get_message(session, chat.id, edited_message.message_id)
        # Nothing places 👀 any more — the Claude meta layer moved out to its
        # own bot on 2026-09-14 — so this only ever matches a row a database
        # already carried. Kept rather than dropped because dropping it would
        # re-open those rows to re-routing, which is the one thing the marker
        # was written to prevent. Read off the row, not off Telegram: a
        # hand-over that committed but failed to place the reaction still
        # holds the point of no return.
        handed_over = previous is not None and previous.receipt_emoji == HANDED_OVER
        was_extracted = previous is not None and previous.extracted_at is not None
        was_fact = previous is not None and previous.verdict == VERDICT_FACT
        previous_verdict = previous.verdict if previous is not None else VERDICT_FACT

    if handed_over:
        # Returning here is also what keeps the receipt: upsert_message
        # never touches receipt_emoji, and the clearing below — which a
        # non-fact verdict would otherwise trigger — is never reached. 👀
        # survives its own edit.
        async with session.begin():
            await store.upsert_message(
                session,
                chat,
                edited_message,
                verdict=previous_verdict,
            )
        log.info("edit after hand-over on %s, ignored", edited_message.message_id)
        return

    # `text or caption`, the same expression `record` and `route` read: an
    # edited photo caption has no `.text`, and reading only that would hand
    # the classifier None — which `presumed` answers `fact`, putting an
    # edited question into the extraction tail and leaving it unanswered.
    verdict = await classify.verdict_for(
        edited_message.text or edited_message.caption,
        continues=await _continues(session, chat.id, edited_message),
    )

    async with session.begin():
        row, _ = await store.upsert_message(
            session,
            chat,
            edited_message,
            verdict=verdict,
        )
        if verdict == VERDICT_FACT:
            # A first sighting as a fact gets the default; a message that
            # was already one gets the next emoji along, and that change is
            # the only thing telling the user the bot saw the edit.
            emoji = next_receipt(row.receipt_emoji) if was_fact else RECEIPT_EMOJI
        else:
            emoji = None
        # This write can clobber a 👀 that `claim` put there microseconds
        # ago, and nothing prevents it. `record` and `record_edited` run
        # fire-and-forget on separate sessions, and both go quiet across a
        # classifier call: an edit arriving while `record` awaits
        # `classify` reads a row that is not yet handed over — so the gate
        # above lets it through — and then lands here *after* `route` has
        # reached `hand_over`. The row ends up with 💔 and a `fact`
        # verdict while Claude is answering the same message: in the
        # extraction tail, wearing a heart, and being talked about.
        #
        # Same class as the provisional-fact window the ledger accepted at
        # Task 5, and the same harm ceiling — spurious facts on a live row,
        # removable by any reaction, never lost data. It needs a human edit
        # inside one model call in a single-user bot. Written down because
        # the marker-clobber half was recorded nowhere: closing it means
        # one row lock across two handlers, which is a bigger change than
        # the race is worth.
        row.receipt_emoji = emoji

        if was_extracted and verdict == VERDICT_FACT:
            report = await extract.run_for(session, chat, config, row)
            log.info("re-extracted message %s: %s fact(s)", row.id, report.facts)
        elif previous is not None:
            # The facts were derived from text that no longer exists, and
            # the row has just left the extraction tail — `unextracted_tail`
            # selects on the verdict — so no pass will ever revisit them.
            # `replace_facts` is what tombstones what an edit dropped, and
            # it is reachable only from a pass, so the row has to do it
            # itself here: the same soft, restorable tombstone the tap
            # gesture places, in the same transaction as the verdict that
            # orphaned them. Without it `/q` goes on counting an expense
            # whose message now reads «а почему это вообще расход», on a
            # message whose 💔 this very edit took off — so nothing on
            # screen says the fact is still there.
            #
            # The predicate is «we have seen this message before», not
            # `was_extracted`. A re-extraction whose model call failed
            # leaves `extracted_at` None (`store.mark_failed` writes only
            # `extract_error`) while the previous pass's facts are still
            # live, so `was_extracted` is a proxy that goes wrong in
            # exactly the state this arm exists for. Asking is cheap and
            # never wrong: `tombstone_facts` selects the live facts itself
            # and stamps nothing on a message that has none.
            count = await store.tombstone_facts(
                session, row.id, row.edited_at or datetime.now(UTC)
            )
            log.info("tombstoned %s fact(s) of edited message %s", count, row.id)

    if emoji is None and previous is not None:
        # The receipt promised a delete gesture the message no longer has.
        # Cleared before `route`, never after: on a fact that became talk
        # the sequence is 💔 → bare → 👀, and the other order would wipe a
        # hand-over receipt the meta layer had just placed.
        #
        # On *any* row we have seen before, not just a fact — the column is
        # not proof of what the bubble shows. The startup sweep clears
        # `receipt_emoji` on a turn its process was killed mid-flight and
        # cannot reach the reaction, so a released row carries NULL under a
        # 👀 that is still on screen. `talk` and `fact` both re-place
        # something over it; a question answers itself and places nothing,
        # and the 👀 would outlive every turn behind it. Telegram has no
        # way to read a message's reactions back, so «clear it anyway» is
        # the only available answer — and on a message that had none it
        # costs one call that changes nothing.
        await clear_receipt(bot, chat.chat_id, edited_message.message_id)

    await route(
        edited_message,
        row,
        chat,
        config,
        session,
        bot,
        hand_over=HAND_OVER,
        receipt=emoji or RECEIPT_EMOJI,
    )
