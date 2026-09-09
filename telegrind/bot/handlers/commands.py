"""Maintenance commands: /import, /rebuild, /reload."""

import logging

from aiogram import flags
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from gspread_asyncio import AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.bot.router import router
from telegrind.importer import ImportedRow, apply_import, plan_import
from telegrind.models import Chat
from telegrind.projection import RebuildReport, rebuild
from telegrind.registry import Registry, invalidate
from telegrind.sheets import invalidate_config

log = logging.getLogger(__name__)

NOT_READY_TEXT = (
    "Сначала пришлите ссылку на таблицу — без неё мне некуда писать. "
    "Отправьте /start, если ссылка потерялась."
)


def format_import_plan(plan: dict[str, list[ImportedRow]]) -> str:
    if not plan:
        return "Нечего импортировать — в листах нет строк."
    lines = ["<b>Импорт истории</b>"]
    for category, rows in sorted(plan.items()):
        synthesized = sum(1 for r in rows if r.synthesized)
        suffix = f" (без ключа: {synthesized})" if synthesized else ""
        lines.append(f"{category}: {len(rows)}{suffix}")
    return "\n".join(lines)


def format_rebuild_report(report: RebuildReport) -> str:
    lines: list[str] = []
    if report.rebuilt:
        lines.append("<b>Перезаписано</b>")
        lines += [f"{ws}: {n}" for ws, n in sorted(report.rebuilt.items())]
    if report.refused:
        lines.append("<b>Отказ</b> — в этих листах есть строки без фактов:")
        lines += [f"{ws}: {n}" for ws, n in sorted(report.refused.items())]
        lines.append(
            "Сначала выполните /import, либо повторите как "
            "<code>/rebuild --force</code>, чтобы затереть эти строки."
        )
    if not lines:
        lines.append("Нечего перезаписывать — в базе нет фактов для этих листов.")
    return "\n".join(lines)


@router.message(Command("import"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def cmd_import(
    message: Message,
    command: CommandObject,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
) -> None:
    """Import pre-bot worksheet rows as facts. `--dry-run` reports only.

    Idempotent on (chat_pk, worksheet, sheet_key), so it is safe to re-run.
    """
    if ags is None or registry is None:
        await message.reply(NOT_READY_TEXT)
        return

    dry_run = "--dry-run" in (command.args or "")
    plan = await plan_import(ags, registry)
    await message.reply(format_import_plan(plan))
    if dry_run or not plan:
        return

    async with session.begin():
        imported, skipped = await apply_import(session, chat, registry, plan)
    await message.reply(f"Импортировано: {imported}, пропущено: {skipped}.")


@router.message(Command("rebuild"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def cmd_rebuild(
    message: Message,
    command: CommandObject,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry | None,
) -> None:
    """Re-project the workbook from fact rows. No LLM cost."""
    if ags is None or registry is None:
        await message.reply(NOT_READY_TEXT)
        return

    force = "--force" in (command.args or "")
    async with session.begin():
        report = await rebuild(ags, session, registry, chat, force=force)
    await message.reply(format_rebuild_report(report))


@router.message(Command("reload"))
async def cmd_reload(message: Message, chat: Chat) -> None:
    """Drop the cached `_categories` and `_config` reads."""
    invalidate(chat.sheet_url)
    invalidate_config(chat.sheet_url)
    await message.reply("Реестр категорий и настройки будут прочитаны заново.")
