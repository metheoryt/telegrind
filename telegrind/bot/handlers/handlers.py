"""Freeform ingestion.

Registration order is match order: the two reply forms first, then voice,
then the catch-all text handler. `edited_message` is a separate observer and
does not compete with them.

The `sheet_url` gate moved. Every handler here writes the message log
*first* and gates on the workbook only before projection. A fact recorded
before onboarding finishes is recoverable with /rebuild; a message dropped
at the handler is gone — and "nothing you write is ever lost" is the
property this design claims.
"""

import logging

from aiogram import Bot, F, flags
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReactionTypeEmoji
from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind import llm, store
from telegrind.bot.router import router
from telegrind.models import Chat, Fact
from telegrind.projection import apply_changes, delete_facts, diff_facts
from telegrind.registry import Registry
from telegrind.sheets import Config

from .start import Form

log = logging.getLogger(__name__)

LINK_MISSING_TEXT = (
    "Ссылка на вашу таблицу потерялась. Пожалуйста, отправьте её мне ещё раз."
)
NOTHING_TEXT = "Ничего не распознала, но сообщение сохранила."
MISSING_TEXT = "Отсутствует в книге..."
VOICE_PENDING_TEXT = (
    "Голосовые пока не расшифровываю, но сообщение сохранила — разберу, когда научусь."
)
ESCALATE_PENDING_TEXT = "Повторный разбор появится в следующей версии."
UNKNOWN_COMMAND_TEXT = (
    "Не знаю такой команды. Сообщение сохранила, но в таблицу "
    "не записала — если это был факт, пришлите его без слэша."
)

#: A slash-prefixed message that no Command filter claimed. Registered
#: immediately before the catch-all so a mistyped /rebiuld is not
#: extracted into the Facts sheet.
COMMAND_LIKE = F.text.startswith("/")


def format_records(facts: list[Fact]) -> str:
    """Echo what was written.

    Every write echoes: the transparency is worth the extra message, and it
    is what makes the edit and delete affordances discoverable.
    """
    if not facts:
        return NOTHING_TEXT

    lines = ["Записано:" if len(facts) == 1 else f"Записей: {len(facts)}"]
    for fact in facts:
        values = " · ".join(str(v) for v in fact.fields.values() if v not in (None, ""))
        lines.append(
            f"<b>{fact.worksheet}</b> · {values} "
            f"<tg-spoiler>{fact.sheet_key}@{fact.worksheet}</tg-spoiler>"
        )
    return "\n".join(lines)


async def _ingest(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    config: Config | None,
    state: FSMContext,
    *,
    model: str | None = None,
) -> None:
    """Log, extract, diff, project, echo."""
    async with session.begin():
        msg_row, _ = await store.upsert_message(session, chat, message)
        message_pk = msg_row.id
        content = msg_row.content
        tg_date = msg_row.tg_date

    if ags is None or registry is None or config is None:
        await state.set_state(Form.request_sheet_url)
        await message.reply(LINK_MISSING_TEXT)
        return

    if not content.strip():
        return

    facts, used_model = await llm.extract(
        content, registry, config, config.localized(tg_date), model
    )

    async with session.begin():
        msg_row = await store.get_message(session, chat.id, message.message_id)
        if msg_row is None:  # cannot happen; the upsert above flushed it
            log.error("message %s vanished between transactions", message.message_id)
            return
        old = await store.facts_for_message(session, message_pk)
        written = await apply_changes(
            ags,
            session,
            registry,
            config,
            chat,
            msg_row,
            diff_facts(old, facts),
            model=used_model,
            prompt_version=llm.PROMPT_VERSION,
        )

    await message.reply(format_records(written))


@router.message(F.reply_to_message & F.text.func(lambda t: t.strip() == "-"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def delete_record(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Delete the replied message's facts. The message row stays.

    The filter is `F.reply_to_message & (text == "-")`, not
    `F.reply_to_message.text`. Today's filter matches *every* reply and then
    falls off the end returning None — the handler matched, so aiogram stops
    propagation, and every reply that is not "-" is silently swallowed.
    """
    if ags is None or registry is None:
        await state.set_state(Form.request_sheet_url)
        await message.reply(LINK_MISSING_TEXT)
        return

    target = message.reply_to_message
    async with session.begin():
        msg_row = await store.get_message(session, chat.id, target.message_id)
        facts = await store.facts_for_message(session, msg_row.id) if msg_row else []
        if not facts:
            await message.reply(MISSING_TEXT)
            return
        await delete_facts(ags, session, registry, facts)

    await bot.set_message_reaction(
        chat_id=target.chat.id,
        message_id=target.message_id,
        reaction=[ReactionTypeEmoji(emoji="💩")],
    )
    await bot.set_message_reaction(
        chat_id=message.chat.id,
        message_id=message.message_id,
        reaction=[ReactionTypeEmoji(emoji="👌")],
    )


@router.message(F.reply_to_message & F.text.func(lambda t: t.strip() == "??"))
async def escalate_stub(message: Message) -> None:
    """Answer a `??` reply instead of recording it.

    Escalated re-extraction is Phase 2. Without this handler the text would
    fall through to record_text and land in the Facts sheet.
    """
    await message.reply(ESCALATE_PENDING_TEXT)


@router.message(F.voice)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_voice(message: Message, chat: Chat, session: AsyncSession) -> None:
    """Log the voice note without transcribing it.

    Transcription is Phase 3, but logging it now means /retranscribe can
    reach back over everything recorded in the meantime.
    """
    async with session.begin():
        await store.upsert_message(session, chat, message)
    await message.reply(VOICE_PENDING_TEXT)


@router.message(COMMAND_LIKE)
async def unknown_command(message: Message, chat: Chat, session: AsyncSession) -> None:
    """Answer an unrecognised command instead of recording it as a fact.

    Without this, /help — or a typo like /rebiuld — falls through to
    record_text and lands in the Facts worksheet. It still logs the message:
    the claim is that nothing you write is ever lost, so declining to
    *extract* something is not licence to drop it.
    """
    async with session.begin():
        await store.upsert_message(session, chat, message)
    await message.reply(UNKNOWN_COMMAND_TEXT)


@router.message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_text(
    message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    config: Config | None,
    state: FSMContext,
) -> None:
    await _ingest(message, chat, session, ags, registry, config, state)


@router.edited_message(F.text)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def record_edited(
    edited_message: Message,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
    config: Config | None,
    state: FSMContext,
) -> None:
    """Re-extract and diff. A category change moves the row between sheets."""
    await _ingest(edited_message, chat, session, ags, registry, config, state)
