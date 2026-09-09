"""/start — an introduction, and nothing to set up.

Onboarding used to ask for a Google Sheets URL and hold the chat in an FSM
state until one arrived, so the first thing the bot ever did was refuse to
record anything. Postgres is the source of truth now and the workbook is an
optional projection, so there is no setup left: the FSM and its
`request_sheet_url` state are gone, and attaching a workbook is the
explicit, later, optional /link.
"""

import logging
from pathlib import Path

from aiogram import flags
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.bot.const import INTRO_TEXT, TIP_TEXT
from telegrind.bot.router import router
from telegrind.models import File

log = logging.getLogger(__name__)

INTRO_FILENAME = "intro.mp4"


@router.message(CommandStart())
@flags.chat_action(action="typing", initial_sleep=0.5)
async def start(message: Message, session: AsyncSession) -> None:
    """Send the intro video, then the tips, and pin the tips.

    The video's file_id is cached in the `file` table so the upload happens
    once per bot deployment rather than once per /start.
    """
    async with session.begin():
        result = await session.execute(
            select(File).where(File.filename == INTRO_FILENAME)
        )
        file = result.scalar_one_or_none()
        video = (
            file.file_id if file else FSInputFile(str(Path("static") / INTRO_FILENAME))
        )

        sent = await message.answer_video(video, caption=INTRO_TEXT)
        if not file:
            session.add(File(file_id=sent.video.file_id, filename=INTRO_FILENAME))

    tips = await message.answer(TIP_TEXT)
    await tips.pin(disable_notification=True)
