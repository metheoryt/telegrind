"""telegrind doctor (SPEC REQ-DEPLOY-1, REQ-DEPLOY-4)."""

import io
import os

from tgkit import user_message

from telegrind.cli import doctor, main
from telegrind.config import Config
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
