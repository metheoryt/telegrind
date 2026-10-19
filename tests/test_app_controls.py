"""Presses, votes and replies to a control are the person's answer (SPEC section 6, REQ-TRIGGER-6, REQ-TRIGGER-7)."""

import pytest
from televibe.telegram.controls import Control, Form, Offer, Poll, control_block
from tgkit import FakeBot, bot_message, callback, poll_vote, user_message

from telegrind.app import App
from telegrind.config import Config
from telegrind.layout import Layout

AT = 1_760_000_000
FORM = Form((("ты", "вы"), ("он", "она")), (None, None))  # no advance tick: a press on «вы» ticks it
POLL = Poll("Что из этого займы?", ("Арман 20 000", "ничего"))


class Recorder:
    def __init__(self):
        self.calls = []

    async def answer(self, uid, message, text, *, addressed=False, answering=None):
        self.calls.append((uid, message.message_id, text, answering))


def make_app(cfg, bot, rec):
    app = App(config=cfg, bot=bot, layout=Layout(cfg.data_dir), answerer=rec, clock=lambda: AT)
    app.rec = rec
    return app


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})


@pytest.fixture
def app(cfg):
    app = make_app(cfg, FakeBot(), Recorder())
    yield app
    app.close()


def seed(app, control: Control | None, message_id: int = 500, poll_id: str | None = None) -> None:
    """A bot answer `message_id` in the log, carrying `control` (None: a plain answer)."""
    store = app.log_for(7)
    raw = bot_message(message_id=message_id, text="Слить 31 пару?").model_dump(mode="json", exclude_none=True)
    store.record_bot_sent(message_id, "Слить 31 пару?", None, raw, AT)
    if control is not None:
        store.record_control(message_id, control_block(control), poll_id, AT)


def kinds(app, message_id: int = 500) -> list[str]:
    return [e.kind for e in app.log_for(7).events(message_id)]


def markups(app) -> list[list[list[str]]]:
    return [
        [[b.text for b in row] for row in kw["reply_markup"].inline_keyboard]
        for name, kw in app.bot.calls
        if name == "edit_message_reply_markup"
    ]


async def test_offer_press_is_logged_then_answered_then_asked(app):
    """REQ-TRIGGER-6, REQ-LOG-1: pressed+answered committed before the callback answer; [✓ Да]; one turn."""
    seed(app, Offer())
    seen = []
    answer_callback_query = app.bot.answer_callback_query

    async def spy(**kwargs):
        seen.append(kinds(app))
        return await answer_callback_query(**kwargs)

    app.bot.answer_callback_query = spy
    await app.on_callback(callback(data="tv:o", message_id=500))
    await app.drain()
    assert seen == [["bot_sent", "control", "pressed", "answered"]]
    assert app.bot.names()[:2] == ["answer_callback_query", "edit_message_reply_markup"]
    assert markups(app) == [[["✓ Да"]]]
    assert app.rec.calls == [(7, 500, "Ответ кнопкой: Да", 500)]  # (uid, message id, text, answering)
    assert not app.log_for(7).control(500).open


async def test_double_press_starts_one_turn(app):
    """REQ-TRIGGER-6: Review Focus 1 — the second press finds the control answered: logged, spinner stopped, no turn."""
    seed(app, Offer())
    await app.on_callback(callback(data="tv:o", query_id="q1"))
    await app.on_callback(callback(data="tv:o", query_id="q2"))
    await app.drain()
    assert app.rec.calls == [(7, 500, "Ответ кнопкой: Да", 500)]
    assert app.bot.names() == ["answer_callback_query", "edit_message_reply_markup", "answer_callback_query"]
    assert kinds(app).count("pressed") == 2 and kinds(app).count("answered") == 1


@pytest.mark.parametrize(
    "query",
    [
        callback(from_uid=8),
        callback(message_id=999),
        callback(message_id=600),
        callback(data="something else"),
        callback().model_copy(update={"message": None}),
    ],
    ids=["stranger", "unknown-message", "no-control", "foreign-data", "no-message"],
)
async def test_press_by_a_stranger_or_on_an_unknown_message_only_stops_the_spinner(app, query):
    """REQ-TRIGGER-6: Review Focus 2 — answer_callback_query, no events, no turn."""
    seed(app, Offer())
    seed(app, None, message_id=600)
    await app.on_callback(query)
    await app.drain()
    assert app.bot.names() == ["answer_callback_query"] and app.rec.calls == []
    assert "pressed" not in kinds(app) + kinds(app, 600) and app.log_for(7).control(500).open
    assert not app.layout.user_dir(8).exists()


async def test_ticks_move_without_a_turn_and_survive_a_restart(app, cfg):
    """REQ-TRIGGER-6: ticks edit the keyboard; a new App on the same data dir submits them with [Готово]."""
    seed(app, FORM)
    await app.on_callback(callback(data="tv:t:0:1"))
    await app.on_callback(callback(data="tv:t:1:0"))
    await app.drain()
    assert markups(app) == [
        [["ты", "✓ вы"], ["он", "она"], ["Готово"]],
        [["ты", "✓ вы"], ["✓ он", "она"], ["Готово"]],
    ]
    assert app.rec.calls == [] and app.log_for(7).control(500).open
    app.close()
    again = make_app(cfg, FakeBot(), Recorder())
    try:
        await again.on_callback(callback(data="tv:d", inaccessible=True))  # a message too old to read: id and chat only
        await again.drain()
        assert again.rec.calls[-1][2] == "Анкета: вы; он"  # Review Focus 5
        assert again.rec.calls == [(7, 500, "Анкета: вы; он", 500)] and markups(again) == [[["✓ Готово"]]]
    finally:
        again.close()


async def test_a_tick_pressed_twice_unticks(app):
    """REQ-TRIGGER-6: pressing the ticked option again takes the tick off, and [Готово] sends «не выбрано»."""
    seed(app, FORM)
    await app.on_callback(callback(data="tv:t:0:1"))
    await app.on_callback(callback(data="tv:t:0:1"))
    await app.on_callback(callback(data="tv:d"))
    await app.drain()
    assert app.rec.calls == [(7, 500, "Анкета: не выбрано; не выбрано", 500)]


async def test_vote_stops_the_poll_and_asks(app):
    """REQ-TRIGGER-6: a vote → stop_poll and a turn with the chosen options; a retraction starts nothing."""
    seed(app, POLL, message_id=501, poll_id="poll-501")
    await app.on_poll_answer(poll_vote(option_ids=()))  # Review Focus 3
    await app.on_poll_answer(poll_vote(uid=8, option_ids=(1,)))  # not an allowed person
    await app.drain()
    assert app.bot.calls == [] and app.rec.calls == [] and app.log_for(7).control(501).open
    assert kinds(app, 501).count("voted") == 1 and not app.layout.user_dir(8).exists()
    await app.on_poll_answer(poll_vote(option_ids=(0,)))
    await app.on_poll_answer(poll_vote(option_ids=(1,)))  # the poll is closed: logged, nothing more
    await app.drain()
    assert app.bot.calls == [("stop_poll", {"chat_id": 7, "message_id": 501})]
    assert app.rec.calls == [(7, 501, "Выбрано в опросе: Арман 20 000", 501)]
    assert not app.log_for(7).control(501).open and kinds(app, 501).count("voted") == 3


async def test_telegram_failures_never_stop_the_turn(cfg):
    """REQ-TRIGGER-6: a refused callback answer, keyboard edit or stop_poll is logged; the turn still starts."""
    bot = FakeBot(fail={"answer_callback_query": 1, "edit_message_reply_markup": 1, "stop_poll": 1})
    app = make_app(cfg, bot, Recorder())
    try:
        seed(app, Offer())
        seed(app, POLL, message_id=501, poll_id="poll-501")
        await app.on_callback(callback(data="tv:o"))
        await app.on_poll_answer(poll_vote(option_ids=(1,)))
        await app.drain()
        assert app.rec.calls == [
            (7, 500, "Ответ кнопкой: Да", 500),
            (7, 501, "Выбрано в опросе: ничего", 501),
        ]
    finally:
        app.close()


def form_reply(text="Максим", message_id=11):
    return user_message(text=text, message_id=message_id, reply_to=bot_message(message_id=500, text="?"))


async def test_text_reply_to_a_form_carries_the_ticks(app):
    """REQ-TRIGGER-7: «Максим» replying to the form → «Анкета: вы; не выбрано\\nТекстом: Максим», control closed."""
    seed(app, FORM)
    await app.on_callback(callback(data="tv:t:0:1"))
    await app.on_message(form_reply())
    await app.drain()
    assert app.rec.calls == [(7, 11, "Анкета: вы; не выбрано\nТекстом: Максим", None)]
    store = app.log_for(7)
    assert not store.control(500).open and store.events(500)[-1].payload == {"how": "text"}
    assert markups(app)[-1] == [["✓ ответ текстом"]]


@pytest.mark.parametrize(
    ("control", "poll_id", "call"),
    [(Offer(), None, "edit_message_reply_markup"), (POLL, "poll-500", "stop_poll")],
    ids=["offer", "poll"],
)
async def test_text_reply_to_an_offer_or_a_poll_closes_it(app, control, poll_id, call):
    """REQ-TRIGGER-7: the reply is asked as typed; the offer's keyboard is marked answered in text, a poll stopped."""
    seed(app, control, poll_id=poll_id)
    await app.on_message(form_reply("не надо"))
    await app.drain()
    assert app.rec.calls == [(7, 11, "не надо", None)] and not app.log_for(7).control(500).open
    assert [n for n, _ in app.bot.calls] == [call]
    await app.on_message(form_reply("и ещё", message_id=12))  # the control is closed: an ordinary reply
    await app.drain()
    assert [n for n, _ in app.bot.calls] == [call] and app.rec.calls[-1] == (7, 12, "и ещё", None)


async def test_voice_reply_to_a_form_carries_the_ticks(app):
    """REQ-TRIGGER-7: the transcript path does the same (Review Focus 4)."""
    seed(app, FORM)
    await app.on_callback(callback(data="tv:t:0:1"))
    await app.on_message(user_message(voice=15, message_id=11, reply_to=bot_message(message_id=500, text="?")))
    assert app.log_for(7).control(500).open  # not settled before the transcript: it is the typed text
    app.log_for(7).record_transcript(11, raw="raw", text="Максим", cleaned=True, at=AT + 1)
    before = len(app.bot.calls)
    await app.on_transcript(7, 11)
    # closed and marked before the note's heart comes off: no Telegram call precedes the settle
    assert app.bot.names()[before:] == ["edit_message_reply_markup", "set_message_reaction"]
    await app.drain()
    assert app.rec.calls == [(7, 11, "Анкета: вы; не выбрано\nТекстом: Максим", None)]
    assert not app.log_for(7).control(500).open and markups(app)[-1] == [["✓ ответ текстом"]]
