from pathlib import Path
import logging

from aiogram import flags, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, FSInputFile
from gspread_asyncio import AsyncioGspreadClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegrind.models import Chat, File
from telegrind.sheets import ConfigSheet
from telegrind.bot.router import router
from telegrind.bot.const import TIP_TEXT

log = logging.getLogger(__name__)


class Form(StatesGroup):
    request_sheet_url = State()


@router.message(CommandStart())
@flags.chat_action(action="typing", initial_sleep=0.5)
async def start(
    message: Message, state: FSMContext, chat: Chat, session: AsyncSession, bot: Bot
):
    await state.set_state(Form.request_sheet_url)

    filename = "intro.mp4"

    async with session.begin():
        result = await session.execute(select(File).where(File.filename == filename))
        file = result.scalar_one_or_none()
        if not file:
            video = FSInputFile(str(Path("static") / Path(filename)))
        else:
            video = file.file_id

        message = await message.answer_video(
            video,
            caption="""\
Пожалуйста, создайте свежий <a href="https://docs.google.com/spreadsheets">Google Sheets документ</a>, \
и поделитесь им со мной:
Укажите в качестве моей почты
<pre>telegrind-bot@telegrind.iam.gserviceaccount.com</pre>
и назначьте меня редактором этой таблицы, чтобы я могла вносить изменения.

Затем скопируйте ссылку и пришлите её мне, чтобы я смогла управлять этим документом.
    """,
        )
        if not file:
            session.add(
                File(
                    file_id=message.video.file_id,
                    filename=filename,
                )
            )


@router.message(Form.request_sheet_url)
@flags.chat_action(action="typing", initial_sleep=0.5)
async def obtain_sheet_url(
    message: Message,
    session: AsyncSession,
    chat: Chat,
    state: FSMContext,
    agc: AsyncioGspreadClient,
    bot: Bot,
):
    sheet_url = message.text
    try:
        ags = await agc.open_by_url(sheet_url)
        await ConfigSheet(ags).get_agw()
    except Exception as e:
        return await message.answer(
            "Не удалось получить доступ к документу. "
            "Убедитесь, что вы выдали мне права редактора, и вышлите ссылку снова. "
            f"Детали: \n{e}"
        )

    # on success
    chat.sheet_url = sheet_url
    await session.commit()

    await state.clear()
    await message.answer(
        f"""Всё круто, теперь вы можете отправлять мне:

{TIP_TEXT}

Всё это я запишу в документ, который вы со мной пошарили.
Все ваши данные остаются у вас, я храню только ссылку на документ."""
    )
    await message.pin(disable_notification=True)
