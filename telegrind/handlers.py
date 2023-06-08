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
from .sheets import Outcome, Loan, ConfigSheet

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
async def start(message: Message, state: FSMContext, chat: Chat, agc: AsyncioGspreadClient):
    # if chat.sheet_url:
    #     ags: AsyncioGspreadSpreadsheet = await agc.open_by_url(chat.sheet_url)
    #     return await message.answer(f'За этим чатом уже закреплён документ "{ags.title}"')

    await state.set_state(Form.request_sheet_url)
    await message.answer(
        """Пожалуйста, создайте свежий Google Sheets документ, и поделитесь им со мной, \
указав в качестве почты <pre>telegrind-bot@telegrind.iam.gserviceaccount.com</pre> и назначив роль редактора.

Затем скопируйте ссылку и пришлите её мне, чтобы я смогла управлять этим документом.
"""
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


@router.message()
async def catchall(message: Message):
    return await message.answer(f'Не поняла... \n\n{TIP_TEXT}')
