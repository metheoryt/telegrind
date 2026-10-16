"""A fake `claude` for the flow tests (SPEC REQ-TEST-3): replays recorded output, records what it was given.

FAKE_TURN_FIXTURE  stdout for a turn (argv has no --json-schema); {{SESSION_ID}} → the --session-id/--resume value
FAKE_ASK_FIXTURE   stdout for a one-shot call (argv has --json-schema)
FAKE_RECORD_DIR    directory; every invocation writes <pid>.json with argv, cwd, env, prompt, pid
FAKE_MODE          hang: start `sleep 600` in the group, then sleep forever
                   pass: write ./facts.sqlite as a pass would (processed rows for every non-bot entry of ./log.sqlite,
                         an `expenses` table with a row per entry whose text opens with a number), then replay
FAKE_STDERR        text to write to stderr after the replay
FAKE_SLEEP         seconds to sleep before the replay
FAKE_EXIT          exit code (default 0)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

AMOUNT = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)")


def _session_id(argv: list[str]) -> str:
    for flag in ("--session-id", "--resume"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    return "no-session-flag"


def _write_facts(cwd: Path) -> None:
    log = sqlite3.connect(f"file:{cwd / 'log.sqlite'}?mode=ro", uri=True)
    rows = log.execute("SELECT message_id, revision, occurred_at, text FROM entries WHERE author != 'bot'").fetchall()
    log.close()
    facts = sqlite3.connect(cwd / "facts.sqlite")
    facts.executescript(
        "CREATE TABLE IF NOT EXISTS processed("
        "entry_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, at INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS keyterms(term TEXT PRIMARY KEY);"
        "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL, at INTEGER NOT NULL,"
        " amount REAL, comment TEXT, currency TEXT, source TEXT NOT NULL DEFAULT 'message', account TEXT);"
    )
    now = int(time.time())
    for message_id, revision, occurred_at, text in rows:
        facts.execute("DELETE FROM expenses WHERE entry_id = ?", (message_id,))
        if text and (m := AMOUNT.match(text)):
            facts.execute(
                "INSERT INTO expenses(entry_id, at, amount, comment, currency) VALUES (?, ?, ?, ?, 'KZT')",
                (message_id, occurred_at, float(m.group(1).replace(",", ".")), text[m.end() :].strip()),
            )
            facts.execute("INSERT OR IGNORE INTO keyterms(term) VALUES (?)", (text[m.end() :].strip() or "трата",))
        facts.execute(
            "INSERT OR REPLACE INTO processed(entry_id, revision, at) VALUES (?, ?, ?)", (message_id, revision, now)
        )
    facts.commit()
    facts.close()


def main() -> int:
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    env = os.environ
    cwd = Path.cwd()
    if "FAKE_RECORD_DIR" in env:
        Path(env["FAKE_RECORD_DIR"]).mkdir(parents=True, exist_ok=True)
        (Path(env["FAKE_RECORD_DIR"]) / f"{os.getpid()}.json").write_text(
            json.dumps(
                {"argv": argv, "cwd": str(cwd), "env": dict(env), "prompt": prompt, "pid": os.getpid()},
                ensure_ascii=False,
            )
        )
    mode = env.get("FAKE_MODE", "")
    time.sleep(float(env.get("FAKE_SLEEP", "0")))
    if mode == "pass":
        _write_facts(cwd)
    fixture = env.get("FAKE_ASK_FIXTURE" if "--json-schema" in argv else "FAKE_TURN_FIXTURE")
    if fixture:
        for line in Path(fixture).read_text().splitlines():
            sys.stdout.write(line.replace("{{SESSION_ID}}", _session_id(argv)) + "\n")
        sys.stdout.flush()
    if "FAKE_STDERR" in env:
        sys.stderr.write(env["FAKE_STDERR"])
        sys.stderr.flush()
    if mode == "hang":
        subprocess.Popen(["sleep", "600"])
        time.sleep(600)
    return int(env.get("FAKE_EXIT", "0"))
