from aiogram import Dispatcher, Router, flags
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message
from gspread import Cell, WorksheetNotFound
from gspread_asyncio import (
    AsyncioGspreadClientManager,
    AsyncioGspreadWorksheet,
    AsyncioGspreadSpreadsheet,
    AsyncioGspreadClient
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from .models import Chat
from .transaction import Conversion, Income, Outcome

dp = Dispatcher()
router = Router()


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

        if chat.sheet_url:
            ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
            data['ags'] = ags

        data['agc'] = agc
        data['chat'] = chat
        data['session'] = session
        return await handler(event, data)


class Form(StatesGroup):
    request_sheet_url = State()


@router.message(CommandStart())
@flags.chat_action(action='typing', initial_sleep=0.5)
async def start(message: Message, state: FSMContext, chat: Chat, agc: AsyncioGspreadClient):
    if chat.sheet_url:
        ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
        return await message.answer(f'За этим чатом уже закреплён документ "{ags.title}"')

    await state.set_state(Form.request_sheet_url)
    await message.answer(
        """Пожалуйста, создайте свежий Google Sheets документ, и поделитесь им со мной, \
указав в качестве почты <pre>telegrind-bot@telegrind.iam.gserviceaccount.com</pre> и назначив роль редактора.

Затем скопируйте ссылку и пришлите её мне, чтобы я смог управлять этим документом.
"""
    )


@router.message(Form.request_sheet_url)
@flags.chat_action(action='typing', initial_sleep=0.5)
async def obtain_sheet_url(message: Message, session: AsyncSession, chat: Chat, state: FSMContext, agc: AsyncioGspreadClient):
    sheet_url = message.text
    try:
        ss = await agc.open_by_url(sheet_url)
        try:
            await ss.worksheet('Base')
        except WorksheetNotFound:
            agw = await ss.add_worksheet('Base', rows=100, cols=100, index=0)
            await agw.update_cells([
                # outcome transaction
                Cell(1, 1, 'Сумма'),
                Cell(1, 2, 'Назначение'),
                Cell(1, 3, 'Валюта'),
                Cell(1, 4, 'Тип'),
                Cell(1, 5, 'Дата')
            ])
            await agw.set_basic_filter('A:E')

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
        'Всё круто, теперь вы можете отправлять мне:\n'
        '<b>Расходы</b>\n'
        '<pre>500</pre> - расход в тенге\n'
        '<pre>101.5 шоколадка</pre> - расход в тенге с комментарием\n'
        '<pre>5.4 USD хостинг</pre> - расход в указаной валюте с комментарием\n'
        '\n'
        '<b>Доходы</b>\n'
        '<pre>+500</pre> - доход в тенге\n'
        '<pre>+500 USD</pre> - доход в указанной валюте\n'
        '<pre>+500 USD зарплата</pre> - доход в указанной валюте с комментарием\n'
        '\n'
        '<b>Конвертации</b>\n'
        '<pre>10 USD &gt; 500000.32 KZT</pre> - перевод из долларов в тенге\n'
        '<pre>10 USD &gt; 500000,46 KZT с халыка на каспи</pre> - перевод из долларов в тенге с комментарием\n'
        '\n'
        '\n'
        'Всё это я запишу в документ, который вы со мной пошарили. '
        'Все ваши данные остаются у вас, я храню только ссылку на документ.'
    )


@router.message()
@flags.chat_action(action='typing', initial_sleep=0.5)
async def record_transaction(message: Message, session: AsyncSession, chat: Chat, ags: AsyncioGspreadSpreadsheet):
    data = None
    for ts in [Conversion, Income, Outcome]:
        try:
            data = ts.parse(message.text)
        except ValueError:
            continue

        agw: AsyncioGspreadWorksheet = await ags.worksheet('Base')
        await ts.record(agw, data)
        return await message.answer('Записал')

    if not data:
        return await message.answer('Не понял')
