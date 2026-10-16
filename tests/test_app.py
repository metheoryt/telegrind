"""The message path: store first, then react, enqueue or ask (SPEC sections 4 and 6, REQ-LOG-1)."""

import pytest
from tgkit import FakeBot, bot_message, reaction, user_message

from telegrind.app import App
from telegrind.config import Config
from telegrind.layout import Layout
from telegrind.triggers import BARE_Q_TEXT


class Recorder:
    def __init__(self):
        self.answers, self.entries = [], []

    async def answer(self, uid, message, text):
        self.answers.append((uid, message.message_id, text))

    def on_entry(self, uid):
        self.entries.append(uid)


class Queue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, uid, inc):
        self.jobs.append((uid, inc.message_id))


@pytest.fixture
def app(tmp_path):
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})
    rec, media_q, voice_q = Recorder(), Queue(), Queue()
    bot = FakeBot()
    app = App(
        config=cfg,
        bot=bot,
        layout=Layout(cfg.data_dir),
        answerer=rec,
        media=media_q,
        voice=voice_q,
        watcher=rec,
        clock=lambda: 1_760_000_000,
    )
    app.rec, app.media_q, app.voice_q = rec, media_q, voice_q
    yield app
    app.close()


async def test_text_is_stored_then_receipted(app):
    """REQ-LOG-1, REQ-RECEIPT-1/3, REQ-LAYOUT-1: entry committed, 💔 set after, receipt remembered, dir created."""
    await app.on_message(user_message(text="4500 такси"))
    log = app.log_for(7)
    entry = log.entry(10)
    assert entry.text == "4500 такси" and entry.receipt == "💔" and entry.revision == 1
    await app.drain()
    assert app.bot.reactions() == [(10, "💔")] and app.rec.answers == [] and app.rec.entries == [7]
    assert (app.layout.user_dir(7) / "media").is_dir() and not app.layout.facts_path(7).exists()


async def test_failed_reaction_still_stores(app, monkeypatch):
    """REQ-RECEIPT-1, REQ-LOG-1: a Telegram error on the reaction is logged; the entry stays, receipt stays None."""
    app.bot.fail["set_message_reaction"] = 1
    await app.on_message(user_message(text="x"))
    assert app.log_for(7).entry(10).receipt is None and app.log_for(7).entry(10).text == "x"

    # Stronger: even an unexpected crash in the reaction must find the entry already committed.
    async def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.bot, "set_message_reaction", boom)
    with pytest.raises(RuntimeError):
        await app.on_message(user_message(text="y", message_id=11))
    assert app.log_for(7).entry(11).text == "y"


async def test_question_gets_no_receipt_and_reaches_the_answerer(app):
    """REQ-TRIGGER-1, REQ-RECEIPT-4: a trailing ? is a question; stored, no heart, handed on with its text."""
    await app.on_message(user_message(text="сколько такси?"))
    await app.drain()
    assert app.log_for(7).entry(10).receipt is None and app.bot.reactions() == []
    assert app.rec.answers == [(7, 10, "сколько такси?")]


async def test_reply_to_bot_is_a_question(app):
    """REQ-TRIGGER-1, REQ-LOG-7: a reply to a stored bot message is a question even without a ?."""
    sent = await app.say(7, "12 000 тенге", reply_to=None)
    assert app.log_for(7).entry(sent.message_id).author == "bot"
    await app.on_message(
        user_message(text="а за сентябрь", message_id=11, reply_to=bot_message(message_id=sent.message_id))
    )
    await app.drain()
    assert app.rec.answers == [(7, 11, "а за сентябрь")]


async def test_slash_q_and_bare_q(app):
    """REQ-TRIGGER-2: /q strips itself; bare /q gets the fixed sentence and starts nothing."""
    await app.on_message(user_message(text="/q сколько"))
    await app.on_message(user_message(text="/q", message_id=11))
    await app.drain()
    assert app.rec.answers == [(7, 10, "сколько")]
    assert app.bot.texts() == [BARE_Q_TEXT]
    assert app.log_for(7).entry(app.bot.sends()[0][1]["chat_id"] and 501).author == "bot"


async def test_start_command_reaches_the_agent(app):
    """REQ-TRIGGER-2: /start is a question whose text is the whole message."""
    await app.on_message(user_message(text="/start"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "/start")]


async def test_photo_and_voice_are_enqueued_after_the_receipt(app):
    """REQ-MSG-2, REQ-VOICE-1: media and voice jobs are enqueued once the entry is committed and receipted."""
    await app.on_message(user_message(photo=True))
    await app.on_message(user_message(voice=15, message_id=11))
    assert app.media_q.jobs == [(7, 10)] and app.voice_q.jobs == [(7, 11)]
    assert app.bot.reactions() == [(10, "💔"), (11, "💔")]


async def test_photo_with_a_question_caption_downloads_and_answers_without_receipt(app):
    """REQ-MSG-2, REQ-RECEIPT-4: a receipt photo asking «сколько тут?» is downloaded and answered; no heart."""
    await app.on_message(user_message(photo=True, caption="сколько тут?"))
    await app.drain()
    assert (
        app.media_q.jobs == [(7, 10)]
        and app.voice_q.jobs == []
        and app.rec.answers == [(7, 10, "сколько тут?")]
        and app.bot.reactions() == []
    )


async def test_edit_moves_the_receipt_and_the_revision(app):
    """REQ-MSG-3, REQ-RECEIPT-2: an edit advances 💔 → ❤‍🔥, bumps revision, starts no turn even if now a question."""
    await app.on_message(user_message(text="4500 такси"))
    await app.on_edit(user_message(text="сколько такси?", edit_date=1_760_000_010))
    entry = app.log_for(7).entry(10)
    assert entry.text == "сколько такси?" and entry.revision == 2 and entry.receipt == "❤‍🔥"
    await app.drain()
    assert app.bot.reactions() == [(10, "💔"), (10, "❤‍🔥")] and app.rec.answers == []


async def test_edit_of_a_question_changes_no_reaction(app):
    """REQ-RECEIPT-2: a question carries no receipt, so its edit sets none."""
    await app.on_message(user_message(text="сколько?"))
    await app.on_edit(user_message(text="сколько всего?", edit_date=1_760_000_010))
    assert app.bot.reactions() == [] and app.log_for(7).entry(10).revision == 2


async def test_reaction_deletes_then_restores(app):
    """REQ-REACT-1, REQ-REACT-2: any emoji → deleted; empty → restored; a repeat writes nothing."""
    await app.on_message(user_message(text="x"))
    await app.on_reaction(reaction(emoji="👍"))
    assert app.log_for(7).entry(10).deleted_at == 1_760_000_000 and app.log_for(7).entry(10).revision == 2
    await app.on_reaction(reaction(emoji="🔥"))
    assert app.log_for(7).entry(10).revision == 2
    await app.on_reaction(reaction(emoji=None))
    assert app.log_for(7).entry(10).deleted_at is None and app.log_for(7).entry(10).revision == 3
    assert app.rec.entries == [7, 7, 7]  # each state change is a new revision the backlog watcher hears about


async def test_reaction_on_unknown_or_bot_message_writes_nothing(app):
    """REQ-REACT-1: a reaction on a message the log never saw, or on the bot's message, is ignored."""
    sent = await app.say(7, "answer", reply_to=None)
    await app.on_reaction(reaction(message_id=sent.message_id, emoji="👍"))
    await app.on_reaction(reaction(message_id=4242, emoji="👍"))
    assert app.log_for(7).entry(sent.message_id).deleted_at is None and app.log_for(7).entry(4242) is None


async def test_stranger_and_group_are_dropped_without_a_trace(app, tmp_path):
    """REQ-ACCESS-1, REQ-ACCESS-2: not an allowed user's private chat → nothing stored, sent or reacted."""
    await app.on_message(user_message(uid=8, text="hi"))
    await app.on_message(user_message(uid=7, text="hi", chat_type="supergroup"))
    await app.on_reaction(reaction(uid=8))
    await app.on_reaction(reaction(uid=7, chat_type="supergroup"))
    await app.on_edit(user_message(uid=8, text="hi", edit_date=1))
    assert app.bot.calls == [] and not (tmp_path / "data" / "users").exists()


async def test_group_message_from_an_allowed_user_is_dropped(app, tmp_path):
    """REQ-ACCESS-2: being allowed does not make a group message the person's private log."""
    await app.on_message(user_message(uid=7, text="4500 такси", chat_type="group"))
    assert app.bot.calls == [] and not (tmp_path / "data" / "users" / "7").exists()


async def test_say_records_the_bot_message_with_parse_mode_none(app):
    """REQ-LOG-7: the bot's text is stored as author bot; model text goes out with parse_mode=None."""
    sent = await app.say(7, "12 000 < 15 000", reply_to=None)
    _name, kw = app.bot.sends()[0]
    assert kw["parse_mode"] is None and kw["text"] == "12 000 < 15 000"
    assert app.log_for(7).entry(sent.message_id).text == "12 000 < 15 000"
    app.bot.fail["send_message"] = 1
    assert await app.say(7, "x", reply_to=None) is None


async def test_a_failing_spawned_task_is_logged_and_drain_survives(app, caplog):
    """A turn that raises must leave an ERROR record; drain() neither raises nor stops the others."""

    async def boom():
        raise ValueError("kaput")

    done = []

    async def fine():
        done.append(1)

    app.spawn(boom())
    app.spawn(fine())
    with caplog.at_level("ERROR", logger="telegrind.app"):
        await app.drain()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "ValueError" in errors[0].getMessage() and "kaput" in errors[0].getMessage()
    assert done == [1]
