from re import Match

from aiogram import Dispatcher, Router, flags, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message
from gspread_asyncio import (
    AsyncioGspreadClientManager,
    AsyncioGspreadSpreadsheet,
    AsyncioGspreadClient
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from .models import Chat
from .transaction import Outcome, Loan

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
        ags = await agc.open_by_url(sheet_url)
        await Outcome.get_ws(ags)
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
        '\n'
        '<b>Расходы</b>\n'
        '<pre>500</pre> - расход в тенге\n'
        '<pre>101.5 шоколадка</pre> - расход в тенге с комментарием\n'
        '<pre>5.4 USD хостинг</pre> - расход в указаной валюте с комментарием\n'
        '\n'
        '<b>Займы</b>\n'
        '<pre>займ Вася Пупкин 500</pre> '
        '- Вася занял <i>(или вернул)</i> у вас 500 тенге сегодня.\n'
        '<pre>займ Вася Пупкин +500</pre> '
        '- Вася вернул <i>(или занял)</i> вам 500 тенге сегодня.\n'
        '<pre>займ Вася Пупкин 10 USD</pre> '
        '- Вася занял у вас 10 долларов.\n'
        '<pre>займ Вася Пупкин 10 USD 01.01.2023</pre> '
        '- Вася занял у вас 10 долларов 1 января 2023-го.\n'
        '<pre>займ Вася Пупкин 10 USD 01.01.2023 на жб ставочку</pre> '
        '- Вася занял у вас 10 долларов 1 января 2023-го на жб ставочку.\n'
        '\n'
        '\n'
        'Всё это я запишу в документ, который вы со мной пошарили. '
        'Все ваши данные остаются у вас, я храню только ссылку на документ.'
    )


@router.message(F.text.regexp(Outcome.pattern).as_("match"))
@flags.chat_action(action='typing', initial_sleep=0.5)
async def record_outcome(message: Message, ags: AsyncioGspreadSpreadsheet, match: Match):
        data = match.groups()
        data = (message.message_id,) + data
        await Outcome.record(ags, data)
        return await message.answer('Записал')


@router.message(F.text.regexp(Loan.pattern).as_("match"))
@flags.chat_action(action='typing', initial_sleep=0.5)
async def record_outcome(message: Message, ags: AsyncioGspreadSpreadsheet, match: Match):
        data = match.groups()
        data = (message.message_id,) + data
        await Loan.record(ags, data)
        return await message.answer('Записал')


@router.message()
async def catchall(message: Message):
    return await message.answer('Не понял')
