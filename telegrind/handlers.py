from pathlib import Path
from re import Match

import aiohttp
import cv2
import numpy as np
from aiogram import Dispatcher, Router, flags, F, Bot
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, PhotoSize, FSInputFile
from gspread_asyncio import (
    AsyncioGspreadClientManager,
    AsyncioGspreadSpreadsheet,
    AsyncioGspreadClient
)
from qreader import QReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from .models import Chat, File
from .sheets import Outcome, Loan, ConfigSheet, Commodity

qr_reader = QReader()


dp = Dispatcher()
router = Router()


TIP_TEXT = """<b>Расходы 💸</b>
---------------
Расход в тенге
<pre>500</pre>

Расход в тенге на шоколадку
<pre>101.5 шоколадка</pre>

Расход в долларах на хостинг
<pre>5.4 USD хостинг</pre>

Расход в долларах за 1 января 2023 на хостинг
<pre>41 USD 01.01.2023 хостинг</pre>


<b>Покупки из чека</>
---------------
Сфотографируйте и отправьте мне QR-код вашего чека, или дайте мне прямую ссылку на чек на сайте consumer.oofd.kz.
Я занесу покупки в отдельный список и запишу общую сумму чека в расход.



<b>Займы ⛓</b>
---------------
Вася занял <i>(или забрал)</i> у вас 500 тенге сегодня
<pre>займ Вася Пупкин 500</pre>

Вася вернул <i>(или занял)</i> вам 500 тенге сегодня
<pre>займ Вася Пупкин +500</pre>

Вася занял у вас 10 долларов сегодня
<pre>займ Вася Пупкин 10 USD</pre>

Вася занял у вас 10 долларов 1 января 2023-го
<pre>займ Вася Пупкин 10 USD 01.01.2023</pre>

Вася занял у вас 10 долларов 1 января 2023-го на жб ставочку
<pre>займ Вася Пупкин 10 USD 01.01.2023 на жб ставочку</pre>"""


@dp.update.middleware()
async def populate_chat_data(handler, event, data):
    async_session: async_sessionmaker[AsyncSession] = data['async_session']
    async with async_session() as session:
        async with session.begin():

            result = await session.execute(
                select(Chat).where(Chat.chat_id == event.event.chat.id)
            )
            chat = result.scalar_one_or_none()
            if not chat:
                chat = Chat(chat_id=event.event.chat.id)
                session.add(chat)

        agcm: AsyncioGspreadClientManager = data['agcm']
        agc: AsyncioGspreadClient = await agcm.authorize()

        data['agc'] = agc
        data['chat'] = chat
        data['session'] = session

        return await handler(event, data)


class Form(StatesGroup):
    request_sheet_url = State()


@router.message(CommandStart())
@flags.chat_action(action='typing', initial_sleep=0.5)
async def start(message: Message, state: FSMContext, chat: Chat, session: AsyncSession, bot: Bot):
    await state.set_state(Form.request_sheet_url)

    filename = 'intro.mp4'

    async with session.begin():
        result = await session.execute(
            select(File).where(File.filename == filename)
        )
        file = result.scalar_one_or_none()
        if not file:
            video = FSInputFile(str(Path('static') / Path(filename)))
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
    """
        )
        if not file:
            session.add(
                File(
                    file_id=message.video.file_id,
                    filename=filename,
                )
            )


@router.message(Form.request_sheet_url)
@flags.chat_action(action='typing', initial_sleep=0.5)
async def obtain_sheet_url(message: Message, session: AsyncSession, chat: Chat, state: FSMContext, agc: AsyncioGspreadClient):
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


@router.message(F.text.regexp(Outcome.pattern))
@flags.chat_action(action='typing', initial_sleep=0.5)
async def record_outcome(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    cfg = ConfigSheet(ags)
    await Outcome(ags).record(message, cfg)
    return await message.reply('Записала!')


@router.message(F.text.regexp(Loan.pattern))
@flags.chat_action(action='typing', initial_sleep=0.5)
async def record_outcome(message: Message, agc: AsyncioGspreadClient, chat: Chat):
    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    cfg = ConfigSheet(ags)
    await Loan(ags).record(message, cfg)
    return await message.reply('Записала!')


@router.message(F.photo)
async def parse_check_by_qr_oofd(message: Message, bot: Bot, agc: AsyncioGspreadClient, chat: Chat):
    ps: PhotoSize = message.photo[-1]
    content = await bot.download(ps.file_id)

    cv2img = cv2.imdecode(np.frombuffer(content.read(), np.uint8), 1)
    image = cv2.cvtColor(cv2img, cv2.COLOR_BGR2RGB)
    decoded_text = qr_reader.detect_and_decode(image=image)
    if not decoded_text:
        return await message.reply("Не нашла ни одного QR кода!")

    urls = [v for v in decoded_text if v and 'oofd.kz' in v]
    if not urls:
        msgs = []
        for i, url in enumerate(decoded_text):
            if not url:
                msgs.append(f"QR #{i+1}: не смогла прочитать код")
            elif 'oofd.kz' not in url:
                msgs.append(f"QR #{i+1}: ко коду не та ссылка")
        return await message.reply('\n'.join(msgs))

    url = urls[0]
    async with aiohttp.ClientSession() as session:
        rs = await session.get(url, ssl=False)
        ticket_token = rs.url.path.split('/')[-1]
        async with session.get(f'https://consumer.oofd.kz/api/tickets/ticket/{ticket_token}', ssl=False) as rs2:
            data = await rs2.json()

    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)

    # write into commodities list and into
    await Commodity(ags).record(message, data)
    await Outcome(ags).write_row(
        Outcome.from_ticket(message, data)
    )

    await message.reply('Записала!')


@router.message(F.text.regexp(r'https?\:\/\/consumer\.oofd\.kz\/ticket\/([0-9a-f\-]{36})').as_('match'))
async def parse_ticket_by_url(message: Message, chat: Chat, agc: AsyncioGspreadClient, match: Match):
    token = match.groups()[0]
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://consumer.oofd.kz/api/tickets/ticket/{token}', ssl=False) as rs2:
            data = await rs2.json()

    ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)

    # write into commodities and expenses lists
    await Commodity(ags).record(message, data)
    await Outcome(ags).write_row(
        Outcome.from_ticket(message, data)
    )

    await message.reply('Записала!')


@router.message()
async def catchall(message: Message):
    return await message.answer(f'Не поняла... \n\n{TIP_TEXT}')
