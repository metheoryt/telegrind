"""Maintenance commands: /link, /import, /rebuild, /reload."""

import logging

from aiogram import flags
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from gspread.exceptions import APIError, NoValidUrlKeyFound
from gspread_asyncio import AsyncioGspreadClient, AsyncioGspreadSpreadsheet
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.bot.const import SERVICE_ACCOUNT_EMAIL
from telegrind.bot.router import router
from telegrind.importer import ImportedRow, apply_import, plan_import
from telegrind.models import Chat
from telegrind.projection import RebuildReport, rebuild
from telegrind.registry import Registry, invalidate
from telegrind.sheets import ConfigSheet, invalidate_config

log = logging.getLogger(__name__)

NOT_READY_TEXT = (
    "Таблица не подключена, так что писать мне некуда — но всё, что вы "
    "присылали, лежит у меня в базе. Пришлите /link, чтобы подключить таблицу."
)

LINK_HELP_TEXT = (
    "Таблица не обязательна: всё, что вы пишете, я храню у себя. "
    "Если хотите видеть данные в Google Sheets:\n\n"
    "1. создайте пустую таблицу;\n"
    "2. дайте права редактора вот на эту почту:\n"
    f"<pre>{SERVICE_ACCOUNT_EMAIL}</pre>\n"
    "3. пришлите <code>/link ссылка-на-таблицу</code>\n\n"
    "Создать таблицу за вас я не могу — у моего служебного аккаунта нет "
    "своего Google Диска. Зато владельцем таблицы остаётесь вы, и удалить "
    "её тоже можете только вы."
)

BAD_URL_TEXT = (
    "Не удалось распознать ссылку. Убедитесь, что скопировали "
    "правильную ссылку на Google Sheets документ."
)

NO_ACCESS_TEXT = (
    "Не удалось получить доступ к документу. Убедитесь, что выдали права "
    f"редактора на <pre>{SERVICE_ACCOUNT_EMAIL}</pre> и пришлите ссылку снова."
)

UNLINKED_TEXT = (
    "Таблица отключена — записывать буду по-прежнему, просто без таблицы. "
    "Не забудьте забрать у меня доступ в самой таблице, если он больше не нужен."
)

NOTHING_LINKED_TEXT = "Таблица и так не подключена."

LINKED_TEXT = (
    "Таблица подключена. Дальше по порядку:\n"
    "/import — если в листах уже есть строки, которые надо забрать в базу "
    "(сначала это, иначе /rebuild откажется их затирать);\n"
    "/rebuild — заполнить таблицу из базы."
)


def format_import_plan(
    plan: dict[str, list[ImportedRow]], refused: dict[str, str] | None = None
) -> str:
    lines: list[str] = []
    for worksheet, err in sorted((refused or {}).items()):
        lines.append(f"<b>Пропущен</b> {worksheet}: {err}")
    if not plan:
        lines.append("Нечего импортировать — в листах нет строк.")
        return "\n".join(lines)
    lines.append("<b>Импорт истории</b>")
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
    if report.failed:
        lines.append("<b>Ошибка</b> — эти листы перезаписать не удалось:")
        lines += [f"{ws}: {err}" for ws, err in sorted(report.failed.items())]
    if not lines:
        lines.append("Нечего перезаписывать — в базе нет фактов для этих листов.")
    return "\n".join(lines)


@router.message(Command("link"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def cmd_link(
    message: Message,
    command: CommandObject,
    chat: Chat,
    session: AsyncSession,
    agc: AsyncioGspreadClient,
) -> None:
    """Attach a workbook, or report the attached one.

    This is what onboarding used to be, minus the FSM: it is a command the
    user reaches for when they want a spreadsheet, not a gate they have to
    pass before the bot will record anything.
    """
    url = (command.args or "").strip()
    if not url:
        if chat.sheet_url:
            await message.reply(f"Сейчас подключена:\n{chat.sheet_url}")
        else:
            await message.reply(LINK_HELP_TEXT)
        return

    try:
        ags = await agc.open_by_url(url)
        # Touching `_config` is the access probe: opening a workbook can
        # succeed on read-only sharing, and writing is the permission that
        # actually matters.
        await ConfigSheet(ags).get_agw()
    except NoValidUrlKeyFound:
        await message.reply(BAD_URL_TEXT)
        return
    except APIError:
        await message.reply(NO_ACCESS_TEXT)
        return

    async with session.begin():
        chat.sheet_url = url
    # The caches are keyed by chat, so a chat that just swapped workbooks
    # would otherwise keep the old one's registry for up to a minute.
    invalidate(str(chat.id))
    invalidate_config(str(chat.id))
    await message.reply(LINKED_TEXT)


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, chat: Chat, session: AsyncSession) -> None:
    """Detach the workbook. Facts stay; only the projection target is dropped.

    This is how the original workbook is retired once its rows have been
    imported: without it the only way to stop writing to a sheet is to link
    a different one.
    """
    if not chat.sheet_url:
        await message.reply(NOTHING_LINKED_TEXT)
        return

    async with session.begin():
        chat.sheet_url = None
    invalidate(str(chat.id))
    invalidate_config(str(chat.id))
    await message.reply(UNLINKED_TEXT)


@router.message(Command("import"))
@flags.chat_action(action="typing", initial_sleep=0.5)
async def cmd_import(
    message: Message,
    command: CommandObject,
    chat: Chat,
    session: AsyncSession,
    ags: AsyncioGspreadSpreadsheet | None,
    registry: Registry,
) -> None:
    """Import pre-bot worksheet rows as facts. `--dry-run` reports only.

    Idempotent on (chat_pk, worksheet, sheet_key), so it is safe to re-run.
    """
    if ags is None:
        await message.reply(NOT_READY_TEXT)
        return

    dry_run = "--dry-run" in (command.args or "")
    plan, refused = await plan_import(ags, registry)
    await message.reply(format_import_plan(plan, refused))
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
    registry: Registry,
) -> None:
    """Re-project the workbook from fact rows. No LLM cost."""
    if ags is None:
        await message.reply(NOT_READY_TEXT)
        return

    force = "--force" in (command.args or "")
    async with session.begin():
        report = await rebuild(ags, session, registry, chat, force=force)
    await message.reply(format_rebuild_report(report))


@router.message(Command("reload"))
async def cmd_reload(message: Message, chat: Chat) -> None:
    """Drop the cached `_categories` and `_config` reads."""
    invalidate(str(chat.id))
    invalidate_config(str(chat.id))
    await message.reply("Реестр категорий и настройки будут прочитаны заново.")
