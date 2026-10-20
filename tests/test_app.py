"""The message path: store first, then react, enqueue or ask (SPEC sections 4 and 6, REQ-LOG-1)."""

import pytest
from tgkit import FakeBot, bot_message, reaction, user_message

from telegrind.app import ADDRESSED_EMPTY, NO_TRANSCRIPT_TEXT, SLOW_TRANSCRIPT_TEXT, App
from telegrind.config import Config
from telegrind.layout import Layout
from telegrind.media import MediaJobs
from telegrind.triggers import WORDLESS_REPLY


class Recorder:
    def __init__(self):
        self.answers, self.entries, self.addressed = [], [], []

    async def answer(self, uid, message, text, *, addressed=False):
        self.answers.append((uid, message.message_id, text))
        self.addressed.append(addressed)

    def on_entry(self, uid):
        self.entries.append(uid)


class Queue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, uid, inc):
        self.jobs.append((uid, inc.message_id))

    async def wait(self, uid, message_id, timeout_s):
        return True


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


async def test_a_trailing_question_mark_is_a_log_entry(app):
    """REQ-TRIGGER-1, REQ-RECEIPT-1: «давление 120/80, нормально?» is stored with its heart and starts nothing."""
    await app.on_message(user_message(text="давление 120/80, нормально?"))
    await app.drain()
    assert app.log_for(7).entry(10).receipt == "💔" and app.bot.reactions() == [(10, "💔")]
    assert app.rec.answers == [] and app.rec.entries == [7]


async def test_a_command_gets_no_receipt_and_reaches_the_answerer(app):
    """REQ-TRIGGER-2, REQ-RECEIPT-4: a question is stored with no heart and handed on with its text."""
    await app.on_message(user_message(text="/start"))
    await app.drain()
    assert app.log_for(7).entry(10).receipt is None and app.bot.reactions() == []
    assert app.rec.answers == [(7, 10, "/start")] and app.rec.addressed == [False]


async def test_reply_to_bot_is_a_question(app):
    """REQ-TRIGGER-1, REQ-LOG-7: a reply to a stored bot message is a question even without a ?."""
    sent = await app.say(7, "12 000 тенге", reply_to=None)
    assert app.log_for(7).entry(sent.message_id).author == "bot"
    await app.on_message(
        user_message(text="а за сентябрь", message_id=11, reply_to=bot_message(message_id=sent.message_id))
    )
    await app.drain()
    assert app.rec.answers == [(7, 11, "а за сентябрь")]


async def test_a_wordless_reply_to_bot_is_a_question(app):
    """REQ-TRIGGER-1: a photo without a caption replying to the bot is asked with the fixed sentence, no heart;
    the same photo not replying to the bot is a log entry."""
    sent = await app.say(7, "Что ты купил в Technodom за 10 030 ₸?", reply_to=None)
    await app.on_message(user_message(photo=True, message_id=11, reply_to=bot_message(message_id=sent.message_id)))
    await app.on_message(user_message(photo=True, message_id=12))
    await app.drain()
    assert app.rec.answers == [(7, 11, WORDLESS_REPLY)]
    assert app.log_for(7).entry(11).receipt is None and app.log_for(7).entry(12).receipt is not None


async def test_q_is_an_ordinary_command(app):
    """REQ-TRIGGER-2: /q has no meaning of its own; it reaches the agent whole, a bare /q included."""
    await app.on_message(user_message(text="/q сколько"))
    await app.on_message(user_message(text="/q", message_id=11))
    await app.drain()
    assert app.rec.answers == [(7, 10, "/q сколько"), (7, 11, "/q")]
    assert app.bot.texts() == []


async def test_start_command_reaches_the_agent(app):
    """REQ-TRIGGER-2: /start is a question whose text is the whole message."""
    await app.on_message(user_message(text="/start"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "/start")]


async def test_photo_and_voice_are_enqueued_once_committed(app):
    """REQ-MSG-2, REQ-VOICE-1: media and voice jobs are enqueued once the entry is committed."""
    await app.on_message(user_message(photo=True))
    await app.on_message(user_message(voice=15, message_id=11))
    assert app.media_q.jobs == [(7, 10)] and app.voice_q.jobs == [(7, 11)]
    assert app.bot.reactions() == [(10, "💔"), (11, "💔")]


async def test_photo_with_a_question_caption_is_a_log_entry_until_addressed(app):
    """REQ-MSG-2, REQ-TRIGGER-4: «сколько тут?» on a photo is stored and downloaded; ❤ then asks with the caption."""
    await app.on_message(user_message(photo=True, caption="сколько тут?"))
    await app.drain()
    assert app.media_q.jobs == [(7, 10)] and app.rec.answers == [] and app.bot.reactions() == [(10, "💔")]
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "сколько тут?")] and app.rec.addressed == [True]


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
    await app.on_message(user_message(text="/q сколько"))
    await app.on_edit(user_message(text="/q сколько всего", edit_date=1_760_000_010))
    assert app.bot.reactions() == [] and app.log_for(7).entry(10).revision == 2


async def test_reaction_deletes_then_restores(app):
    """REQ-REACT-1, REQ-REACT-2: a receipt heart → deleted; another heart → no change; none → restored."""
    await app.on_message(user_message(text="x"))
    await app.on_reaction(reaction(emoji="💔"))
    assert app.log_for(7).entry(10).deleted_at == 1_760_000_000 and app.log_for(7).entry(10).revision == 2
    await app.on_reaction(reaction(emoji="💘", old="💔"))
    assert app.log_for(7).entry(10).revision == 2
    await app.on_reaction(reaction(emoji=None, old="💘"))
    assert app.log_for(7).entry(10).deleted_at is None and app.log_for(7).entry(10).revision == 3
    assert app.rec.entries == [7, 7, 7]  # each state change is a new revision the backlog watcher hears about
    assert app.rec.answers == []


@pytest.mark.parametrize("emoji", ["👍", "🔥", "🙏"])
async def test_other_emoji_delete_nothing_and_ask_nothing(app, emoji):
    """REQ-REACT-1: only a receipt heart deletes; other emoji change nothing."""
    await app.on_message(user_message(text="x"))
    await app.on_reaction(reaction(emoji=emoji))
    await app.drain()
    assert app.log_for(7).entry(10).deleted_at is None and app.log_for(7).entry(10).revision == 1
    assert app.rec.answers == []


async def test_heart_addresses_an_entry_and_takes_it_out_of_the_log(app):
    """REQ-TRIGGER-4, REQ-RECEIPT-4: ❤ starts an addressed turn; the entry is marked addressed and loses 💔."""
    await app.on_message(user_message(text="вчера было не 500, а 5000"))
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    entry = app.log_for(7).entry(10)
    assert app.rec.answers == [(7, 10, "вчера было не 500, а 5000")] and app.rec.addressed == [True]
    assert entry.addressed_at == 1_760_000_000 and entry.receipt is None and entry.revision == 2
    assert app.bot.reactions()[-1][1] is None and app.rec.entries == [7, 7]  # stored, then addressed


async def test_removing_the_heart_returns_the_entry_to_the_log(app):
    """REQ-TRIGGER-4, REQ-RECEIPT-4: ❤ taken off → no longer addressed, 💔 back, nothing asked."""
    await app.on_message(user_message(text="такси 4500"))
    await app.on_reaction(reaction(emoji="❤"))
    await app.on_reaction(reaction(emoji=None, old="❤"))
    await app.drain()
    entry = app.log_for(7).entry(10)
    assert entry.addressed_at is None and entry.receipt == "💔" and entry.revision == 3
    assert app.rec.answers == [(7, 10, "такси 4500")] and app.rec.entries == [7, 7, 7]


async def test_removing_the_heart_from_a_question_sets_no_receipt(app):
    """REQ-RECEIPT-4: a question was never a log entry, so taking ❤ off it gives it no 💔."""
    await app.on_message(user_message(text="/start"))
    await app.on_reaction(reaction(emoji="❤"))
    await app.on_reaction(reaction(emoji=None, old="❤"))
    await app.drain()
    assert app.log_for(7).entry(10).receipt is None and "💔" not in [r[1] for r in app.bot.reactions()]


async def test_heart_with_a_variation_selector_addresses_too(app):
    """REQ-TRIGGER-4: ❤️ (U+2764 U+FE0F) is the same reaction as ❤."""
    await app.on_message(user_message(text="x"))
    await app.on_reaction(reaction(emoji="\u2764\ufe0f"))
    await app.drain()
    assert app.rec.addressed == [True]


async def test_heart_kept_or_removed_starts_nothing(app):
    """REQ-TRIGGER-4: only putting ❤ counts."""
    await app.on_message(user_message(text="x"))
    await app.on_reaction(reaction(emoji="❤", old="❤"))
    await app.on_reaction(reaction(emoji=None, old="❤"))
    await app.drain()
    assert app.rec.answers == []


async def test_heart_on_a_deleted_entry_restores_and_addresses_it(app):
    """REQ-REACT-1, REQ-TRIGGER-4: ❤ replacing the person's 💔 restores the entry and asks."""
    await app.on_message(user_message(text="x"))
    await app.on_reaction(reaction(emoji="💔"))
    await app.on_reaction(reaction(emoji="❤", old="💔"))
    await app.drain()
    assert app.log_for(7).entry(10).deleted_at is None and app.rec.answers == [(7, 10, "x")]


async def test_heart_on_a_forward_and_on_a_bare_photo(app):
    """REQ-TRIGGER-4: a forward is addressable; an entry with no text is asked with the fixed sentence."""
    await app.on_message(user_message(text="курс 520", forward=("user", "Иван")))
    await app.on_message(user_message(photo=True, message_id=11))
    await app.on_reaction(reaction(emoji="❤"))
    await app.on_reaction(reaction(message_id=11, emoji="❤"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "курс 520"), (7, 11, ADDRESSED_EMPTY)]


async def test_heart_on_a_voice_note_and_on_its_transcript_quote(app):
    """REQ-VOICE-7, REQ-TRIGGER-4: ❤ on the note or on the bot's quote of it asks with the clean transcript."""
    await app.on_message(user_message(voice=15))
    app.log_for(7).record_transcript(10, raw="raw", text="сколько я потратил", cleaned=True, at=1_760_000_001)
    quote = await app.say(7, "🎙 сколько я потратил", reply_to=10)
    await app.on_reaction(reaction(emoji="❤"))
    await app.on_reaction(reaction(message_id=quote.message_id, emoji="❤"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "сколько я потратил"), (7, 10, "сколько я потратил")]


async def test_heart_on_a_transcript_quote_takes_the_voice_note_out_of_the_log(app):
    """REQ-TRIGGER-4: ❤ on the quote addresses the note it quotes; taking it off returns the note to the log."""
    await app.on_message(user_message(voice=15))
    app.log_for(7).record_transcript(10, raw="raw", text="заправился на 15000", cleaned=True, at=1_760_000_001)
    quote = await app.say(7, "🎙 заправился на 15000", reply_to=10)
    await app.on_reaction(reaction(message_id=quote.message_id, emoji="❤"))
    note = app.log_for(7).entry(10)
    assert note.addressed_at == 1_760_000_000 and note.receipt is None
    await app.on_reaction(reaction(message_id=quote.message_id, emoji=None, old="❤"))
    await app.drain()
    note = app.log_for(7).entry(10)
    assert note.addressed_at is None and note.receipt == "💔"


async def test_another_emoji_on_the_quote_leaves_the_note_addressed(app):
    """REQ-TRIGGER-4: only ❤ being put or taken off changes anything; 👍 on the quote does not."""
    await app.on_message(user_message(voice=15))
    app.log_for(7).record_transcript(10, raw="raw", text="заправился", cleaned=True, at=1_760_000_001)
    quote = await app.say(7, "🎙 заправился", reply_to=10)
    await app.on_reaction(reaction(emoji="❤"))
    await app.on_reaction(reaction(message_id=quote.message_id, emoji="👍"))
    await app.drain()
    assert app.log_for(7).entry(10).addressed_at == 1_760_000_000


async def test_heart_before_the_transcript_waits_for_it(app):
    """REQ-VOICE-7: ❤ on a note still being transcribed waits for the transcript, then asks with it."""
    await app.on_message(user_message(voice=15))

    async def finished(uid, message_id, timeout_s):
        app.log_for(7).record_transcript(10, raw="raw", text="сколько я потратил", cleaned=True, at=1_760_000_001)
        return True

    app.voice_q.wait = finished
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "сколько я потратил")] and app.bot.texts() == []


async def test_heart_taken_off_while_the_transcript_is_awaited_starts_nothing(app):
    """REQ-VOICE-7: the turn starts only if the note is still addressed when its transcript arrives."""
    await app.on_message(user_message(voice=15))

    async def finished(uid, message_id, timeout_s):
        await app.on_reaction(reaction(emoji=None, old="❤"))
        app.log_for(7).record_transcript(10, raw="raw", text="сколько я потратил", cleaned=True, at=1_760_000_001)
        return True

    app.voice_q.wait = finished
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    assert app.rec.answers == [] and app.bot.texts() == []


async def test_heart_on_a_voice_note_that_was_not_transcribed_says_so(app):
    """REQ-VOICE-7: the job ended with no transcript → the fixed sentence, no turn."""
    await app.on_message(user_message(voice=15))
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    assert app.rec.answers == [] and app.bot.texts() == [NO_TRANSCRIPT_TEXT]


async def test_heart_on_a_voice_note_still_transcribing_after_the_wait_says_so(app):
    """REQ-VOICE-7: the transcript did not come in time → a sentence saying so, no turn."""
    await app.on_message(user_message(voice=15))

    async def running(uid, message_id, timeout_s):
        return False

    app.voice_q.wait = running
    await app.on_reaction(reaction(emoji="❤"))
    await app.drain()
    assert app.rec.answers == [] and app.bot.texts() == [SLOW_TRANSCRIPT_TEXT]


async def test_heart_on_a_bot_answer_is_ignored(app):
    """REQ-TRIGGER-4: ❤ on a bot message that is not a transcript quote does nothing."""
    await app.on_message(user_message(text="/start"))
    sent = await app.say(7, "answer", reply_to=10)
    await app.on_reaction(reaction(message_id=sent.message_id, emoji="❤"))
    await app.drain()
    assert app.rec.answers == [(7, 10, "/start")]


async def test_reaction_on_unknown_or_bot_message_writes_nothing(app):
    """REQ-REACT-1: a reaction on a message the log never saw, or on the bot's message, is ignored."""
    sent = await app.say(7, "answer", reply_to=None)
    await app.on_reaction(reaction(message_id=sent.message_id, emoji="💔"))
    await app.on_reaction(reaction(message_id=4242, emoji="💔"))
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


def media_status(app, message_id=10):
    row = app.log_for(7).conn.execute("SELECT status FROM media WHERE message_id = ?", (message_id,)).fetchone()
    return None if row is None else row["status"]


async def test_a_crash_in_the_reaction_does_not_lose_the_media_job(app, monkeypatch):
    """REQ-LOG-1, REQ-MSG-2: the job row is written before any await, so resume() finds it whatever happens next."""
    app.media = MediaJobs(app)

    async def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.bot, "set_message_reaction", boom)
    with pytest.raises(RuntimeError):
        await app.on_message(user_message(photo=True))
    assert media_status(app) == "pending"
    await app.drain()


class BrokenWatcher:
    def on_entry(self, uid):
        raise TypeError("int() argument must be a string, not 'NoneType'")


async def test_a_failing_backlog_check_does_not_lose_the_message_or_its_job(app, caplog):
    """Agent-owned data that breaks the backlog check (a NULL revision) is logged; the entry, receipt, job stand."""
    app.media, app.watcher = MediaJobs(app), BrokenWatcher()
    await app.on_message(user_message(photo=True))
    entry = app.log_for(7).entry(10)
    assert entry is not None and entry.receipt == "💔" and media_status(app) == "pending"
    assert any(
        r.levelname in ("WARNING", "ERROR") and "TypeError" in r.getMessage() and "7" in r.getMessage()
        for r in caplog.records
    )
    await app.drain()
