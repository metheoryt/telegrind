"""telegrind doctor (SPEC REQ-DEPLOY-1, REQ-DEPLOY-4)."""

import io
import os
import signal

from aiogram import Dispatcher
from tgkit import user_message

from telegrind import cli
from telegrind.cli import doctor, main
from telegrind.config import Config
from telegrind.facts import Facts, FactsUnavailable
from telegrind.incoming import incoming
from telegrind.layout import Layout
from telegrind.log import Log


def test_doctor_reports_binaries_ownership_and_counts(tmp_path, fake_path, monkeypatch):
    """REQ-DEPLOY-1, REQ-DEPLOY-4: versions of claude and sqlite3, who owns the dirs, counts per person."""
    cfg = Config.from_env(
        {
            "BOT_TOKEN": "t",
            "ALLOWED_USER_IDS": "7",
            "DATA_DIR": str(tmp_path / "data"),
            "ACCOUNT_HOME": str(tmp_path / "acc"),
        }
    )
    layout = Layout(cfg.data_dir)
    layout.ensure_user(7)
    (tmp_path / "acc").mkdir()
    log = Log(layout.log_path(7))
    log.record_received(incoming(user_message(text="4500 такси"), now=1))
    log.media_enqueue(10, "f", "a.jpg", "image/jpeg", 1)
    log.voice_enqueue(11, "v")
    log.close()
    out = io.StringIO()
    assert doctor(cfg, out) == 0
    text = out.getvalue()
    assert "claude" in text and "sqlite3" in text and f"uid {os.getuid()}" in text
    assert "user 7: entries 1, unprocessed 1" in text and "media: pending 1" in text and "voice: pending 1" in text


def test_doctor_fails_without_the_binaries(tmp_path, monkeypatch):
    """REQ-DEPLOY-1: a missing claude or sqlite3 is exit 1."""
    monkeypatch.setenv("PATH", str(tmp_path))
    cfg = Config.from_env(
        {"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data"), "ACCOUNT_HOME": str(tmp_path)}
    )
    assert doctor(cfg, io.StringIO()) == 1


def test_main_doctor_reads_the_environment(tmp_path, fake_path, monkeypatch, capsys):
    """REQ-DEPLOY-3: `telegrind doctor` takes its config from the environment and refuses without ALLOWED_USER_IDS."""
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    assert main(["doctor"]) == 2 and "ALLOWED_USER_IDS" in capsys.readouterr().err


def test_doctor_reports_unreadable_facts(tmp_path, fake_path, monkeypatch):
    """doctor is where an unreadable facts.sqlite should show, not where it should crash."""
    cfg = Config.from_env(
        {"BOT_TOKEN": "t", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data"), "ACCOUNT_HOME": str(tmp_path)}
    )
    Layout(cfg.data_dir).ensure_user(7)

    def unavailable(self):
        raise FactsUnavailable("OperationalError")

    monkeypatch.setattr(Facts, "processed", unavailable)
    out = io.StringIO()
    doctor(cfg, out)
    assert "user 7:" in out.getvalue() and "unavailable (OperationalError)" in out.getvalue()


async def test_run_leaves_sigterm_to_our_handler(tmp_path, monkeypatch):
    """REQ-TURN-9: aiogram's own SIGTERM handler (a graceful stop, then a drain that can outlast compose's grace
    period) would replace the os._exit one; polling starts with handle_signals=False so ours stands."""
    seen = {}

    async def start_polling(self, *bots, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(Dispatcher, "start_polling", start_polling)
    cfg = Config.from_env({"BOT_TOKEN": "1:x", "ALLOWED_USER_IDS": "7", "DATA_DIR": str(tmp_path / "data")})
    await cli.run(cfg)
    assert seen.get("handle_signals") is False
    # REQ-TRIGGER-6: a press and a vote are delivered only when asked for
    assert {"callback_query", "poll_answer"} <= set(seen["allowed_updates"])


def test_sigterm_handler_exits_at_once(monkeypatch):
    """REQ-TURN-9: SIGTERM is os._exit(0): no cancellation, so televibe's markers stay for recover()."""
    installed, exits = {}, []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.update({sig: handler}))
    monkeypatch.setattr(os, "_exit", exits.append)
    cli._exit_on_sigterm()
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert exits == [0]
