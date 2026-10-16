"""Voice: Scribe, cleanup through the one-shot call, the quote post (SPEC section 5)."""

import json
import sqlite3

import httpx
import pytest
from conftest import FIXTURES, records, turn_env
from televibe import Engine
from tgkit import FakeBot, user_message

from telegrind.agent import Agent, make_provider
from telegrind.app import App
from telegrind.config import Config
from telegrind.keyterms import filter_terms
from telegrind.layout import Layout
from telegrind.voice import MAX_SECONDS, PREFIX, VoiceJobs, chunks, quote

RAW = "четыре пятьсот такси вчера ну вечером"
QUESTION_RAW = "сколько я потратил на такси в сентябре"


def test_filter_terms():
    """REQ-VOICE-3: under 50 chars, at most 5 words, no <>{}[]\\, deduped case-insensitively, at most 100."""
    terms = ["такси", "Такси", "a b c d e f", "x" * 50, "bad<term>", "Kaspi", *[f"t{i}" for i in range(200)]]
    out = filter_terms(terms)
    assert out[:2] == ["такси", "Kaspi"] and len(out) == 100 and "a b c d e f" not in out and "bad<term>" not in out


def test_chunks_and_quote():
    """REQ-VOICE-6: prefix, split between paragraphs then words at 4096 UTF-16 units, folded quote above 500."""
    assert chunks("короткий") == [PREFIX + "короткий"]
    parts = chunks("а" * 5000)
    assert (
        len(parts) == 2 and all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts) and parts[0].startswith(PREFIX)
    )
    assert quote("x" * 10)[0].type == "blockquote" and quote("x" * 600)[0].type == "expandable_blockquote"
    assert quote("😀😀")[0].length == 4  # UTF-16 units


def job(app, columns="status"):
    """The voice_jobs row of message 10."""
    return app.log_for(7).conn.execute(f"SELECT {columns} FROM voice_jobs WHERE message_id = 10").fetchone()


class Scribe:
    """A fake ElevenLabs endpoint: records requests, answers with `text`, or fails `fail` times."""

    def __init__(self, text=RAW, fail=0, status=500):
        self.text, self.fail, self.status, self.requests = text, fail, status, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail > 0:
            self.fail -= 1
            return httpx.Response(self.status)
        return httpx.Response(200, json={"text": self.text})


@pytest.fixture
async def rig(tmp_path, fake_path):
    cfg = Config.from_env(
        {
            "BOT_TOKEN": "t",
            "ALLOWED_USER_IDS": "7",
            "DATA_DIR": str(tmp_path / "data"),
            "ACCOUNT_HOME": str(tmp_path / "acc"),
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            "ELEVENLABS_API_KEY": "el",
        }
    )
    scribe = Scribe()
    client = httpx.AsyncClient(transport=httpx.MockTransport(scribe.handler))
    slept = []

    async def sleep(s):
        slept.append(s)

    async with Engine(tmp_path / "state") as engine:
        app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir), clock=lambda: 1_760_000_000)
        agent = Agent(config=cfg, app=app, engine=engine, provider=make_provider(cfg), turn_env=turn_env(tmp_path))
        app.answerer = agent
        app.voice = VoiceJobs(app, agent=agent, client=client, sleep=sleep)
        app.voice.ask_env = turn_env(tmp_path)
        app.scribe, app.slept = scribe, slept
        yield app
        await app.drain()
        app.close()
        await client.aclose()


def ask_text(name="ask.jsonl"):
    """The structured output the fake one-shot call answers with, from the named recording."""
    for line in (FIXTURES / name).read_text().splitlines():
        rec = json.loads(line)
        if rec.get("type") == "result":
            return rec["structured_output"]
    raise AssertionError(f"{name} has no result")


async def test_voice_is_transcribed_cleaned_posted_and_stored(rig, tmp_path):
    """REQ-VOICE-1, -2, -5, -6: upload (never a link), raw and clean kept, quote posted as bot_sent."""
    app = rig
    app.bot.files["v1"] = b"OGG"
    out = ask_text()
    assert out["question"] is False and out["text"] != RAW  # the recording this test is about
    await app.on_message(user_message(voice=15))
    await app.drain()
    req = app.scribe.requests[0]
    assert req.headers["xi-api-key"] == "el" and b"OGG" in req.content and b"scribe_v2" in req.content
    assert b"http" not in req.content and b"files/v1" not in req.content  # the bytes, never the Telegram file URL
    entry = app.log_for(7).entry(10)
    assert entry.transcript_raw == RAW and entry.transcript == out["text"]
    event = next(e for e in app.log_for(7).events(10) if e.kind == "transcribed")
    assert event.payload == {"raw": RAW, "text": out["text"], "question": False, "cleaned": True}
    _, kw = app.bot.sends()[0]
    assert kw["text"] == PREFIX + out["text"] and kw["parse_mode"] is None
    assert kw["entities"][0].type in ("blockquote", "expandable_blockquote") and kw["reply_parameters"].message_id == 10
    assert app.log_for(7).entry(501).author == "bot"
    assert [e.kind for e in app.log_for(7).events(501)] == ["bot_sent"]
    assert job(app)["status"] == "done"
    rec = records(tmp_path)[0]
    assert "--json-schema" in rec["argv"] and RAW in rec["prompt"]
    assert app.bot.reactions() == [(10, "💔")]


async def test_bot_secrets_never_reach_the_cleanup_call(rig, tmp_path):
    """REQ-VOICE-2: ELEVENLABS_API_KEY and BOT_TOKEN never enter the one-shot call's environment."""
    app = rig
    app.voice.ask_env = {**turn_env(tmp_path), "BOT_TOKEN": "x", "ELEVENLABS_API_KEY": "y"}
    await app.on_message(user_message(voice=15))
    await app.drain()
    rec = next(r for r in records(tmp_path) if "--json-schema" in r["argv"])
    assert "BOT_TOKEN" not in rec["env"] and "ELEVENLABS_API_KEY" not in rec["env"]
    assert rec["env"]["DISABLE_AUTOUPDATER"] == "1"  # TURN_ENV is merged in
    assert app.log_for(7).entry(10).transcript == ask_text()["text"]  # the call ran and was not refused


async def test_voice_question_flag_starts_a_turn_after_the_post(rig, tmp_path):
    """REQ-VOICE-7: question true → an agent turn with the clean text, after the quote is posted."""
    app = rig
    out = ask_text("ask_question.jsonl")
    assert out["question"] is True
    app.scribe.text = QUESTION_RAW
    app.voice.ask_env = turn_env(tmp_path, ask="ask_question.jsonl")
    await app.on_message(user_message(voice=15))
    await app.drain()
    names = app.bot.names()
    first_send = names.index("send_message")
    assert app.bot.texts()[0] == PREFIX + out["text"]
    assert "set_message_reaction" in names[first_send:]  # the turn's 👀 comes after the quote
    turn = next(r for r in records(tmp_path) if "--json-schema" not in r["argv"])
    assert turn["prompt"] == out["text"]


async def test_cleanup_failure_posts_the_raw_text_and_asks_nothing(rig, tmp_path):
    """REQ-VOICE-5: the cleaning call fails → raw text, cleaned false, question false; a transcript beats none."""
    app = rig
    app.voice.ask_env = turn_env(tmp_path, ask="turn.jsonl")  # a result with no structured_output → AskFailed
    await app.on_message(user_message(voice=15))
    await app.drain()
    entry = app.log_for(7).entry(10)
    assert entry.transcript == RAW and entry.transcript_raw == RAW
    event = next(e for e in app.log_for(7).events(10) if e.kind == "transcribed")
    assert event.payload == {"raw": RAW, "text": RAW, "question": False, "cleaned": False}
    assert app.bot.texts() == [PREFIX + RAW]


async def test_scribe_failure_retries_then_records_the_reason(rig):
    """REQ-VOICE-8: backoff up to 5 attempts; a persisting failure is in voice_jobs; nothing is posted."""
    app = rig
    app.scribe.fail = 99
    await app.on_message(user_message(voice=15))
    await app.drain()
    row = job(app, "status, attempts, last_error")
    assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 5, "ScribeError: HTTP 500")
    assert app.bot.texts() == [] and app.slept == [1, 2, 4, 8]
    assert app.log_for(7).entry(10).transcript is None


async def test_download_failure_logs_the_type_only(rig, caplog):
    """REQ-VOICE-2: the file URL carries the bot token, so a download error records the exception type alone."""
    app = rig
    app.bot.fail["get_file"] = 99
    await app.on_message(user_message(voice=15))
    await app.drain()
    row = job(app, "status, last_error")
    assert (row["status"], row["last_error"]) == ("failed", "TelegramBadRequest")
    assert "rejected" not in caplog.text and app.scribe.requests == []


async def test_too_long_is_not_transcribed(rig):
    """REQ-VOICE-1: over 20 minutes → too_long, no upload."""
    app = rig
    await app.on_message(user_message(voice=MAX_SECONDS + 1))
    await app.drain()
    assert job(app)["status"] == "too_long"
    assert app.scribe.requests == []


async def test_voice_without_facts_db_runs_with_no_keyterms(rig):
    """REQ-VOICE-3: no facts.sqlite yet → no keyterms in the form, and the job still runs."""
    app = rig
    assert not app.layout.facts_path(7).exists()
    await app.on_message(user_message(voice=15))
    await app.drain()
    assert b"keyterms" not in app.scribe.requests[0].content and app.log_for(7).entry(10).transcript_raw == RAW


async def test_keyterms_come_from_facts(rig):
    """REQ-VOICE-3: terms from facts.sqlite's keyterms, filtered."""
    app = rig
    app.layout.ensure_user(7)
    conn = sqlite3.connect(app.layout.facts_path(7))
    conn.execute("CREATE TABLE keyterms(term TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO keyterms VALUES (?)", [("такси",), ("Kaspi",), ("bad<x>",)])
    conn.commit()
    conn.close()
    await app.on_message(user_message(voice=15))
    await app.drain()
    body = app.scribe.requests[0].content.decode("utf-8", "replace")
    assert "такси" in body and "Kaspi" in body and "bad<x>" not in body


async def test_resume_runs_pending_jobs(rig):
    """A pending job left by a restart runs again from voice_jobs."""
    app = rig
    voice, app.voice = app.voice, None
    await app.on_message(user_message(voice=15))  # stored, no job queued
    app.log_for(7).voice_enqueue(10, "v1")
    app.voice = voice
    voice.resume()
    await app.drain()
    assert app.log_for(7).entry(10).transcript_raw == RAW


async def test_without_elevenlabs_key_voice_is_stored_only(tmp_path, fake_path):
    """REQ-VOICE-2: no key → stored and receipted, job skipped, nothing uploaded."""
    cfg = Config.from_env({"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})
    app = App(config=cfg, bot=FakeBot(), layout=Layout(cfg.data_dir))
    scribe = Scribe()
    client = httpx.AsyncClient(transport=httpx.MockTransport(scribe.handler))
    app.voice = VoiceJobs(app, agent=None, client=client)
    await app.on_message(user_message(voice=15))
    await app.drain()
    assert job(app)["status"] == "skipped"
    assert scribe.requests == [] and app.bot.reactions() == [(10, "💔")]
    app.close()
    await client.aclose()
