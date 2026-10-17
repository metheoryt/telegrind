"""`telegrind run` and `telegrind doctor`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from typing import TextIO

import httpx

from telegrind.config import Config, ConfigError
from telegrind.facts import Facts, FactsUnavailable
from telegrind.layout import Layout
from telegrind.log import Log

log = logging.getLogger(__name__)


def _version(binary: str) -> str | None:
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return f"{path} (version unknown)"
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else path


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"


def doctor(config: Config, out: TextIO) -> int:
    """REQ-DEPLOY-1, REQ-DEPLOY-4: binaries, ownership, counts. 1 when a binary is missing. Prints no secret."""
    code = 0
    for binary in ("claude", "sqlite3"):
        version = _version(binary)
        out.write(f"{binary}: {version or 'MISSING'}\n")
        if version is None:
            code = 1
    me = os.getuid()
    for name, path in (("DATA_DIR", config.data_dir), ("ACCOUNT_HOME", config.account_home)):
        if path.exists():
            st = path.stat()
            owned = "ok" if st.st_uid == me else "NOT OURS"
            out.write(f"{name} {path}: owner uid {st.st_uid}, we are uid {me} ({owned})\n")
        else:
            out.write(f"{name} {path}: missing\n")
    out.write(
        f"claude token: {'set' if config.claude_token else 'absent'}; "
        f"elevenlabs key: {'set' if config.elevenlabs_key else 'absent'}\n"
    )
    layout = Layout(config.data_dir)
    for uid in layout.users():
        store = Log(layout.log_path(uid))
        try:
            facts = Facts(layout.facts_path(uid))
            try:
                unprocessed = str(len(store.unprocessed(facts.processed())))
            except FactsUnavailable as exc:
                unprocessed = f"unknown, facts.sqlite unavailable ({exc.reason})"
            out.write(
                f"user {uid}: entries {store.count_entries()}, unprocessed {unprocessed}; "
                f"media: {_fmt(store.media_counts())}; voice: {_fmt(store.voice_counts())}; "
                f"facts.sqlite {'present' if facts.exists() else 'absent'}\n"
            )
        finally:
            store.close()
    return code


async def run(config: Config) -> None:
    from aiogram import Bot
    from televibe import Engine

    from telegrind.agent import Agent, make_provider
    from telegrind.app import App
    from telegrind.bot import build_dispatcher
    from telegrind.media import MediaJobs
    from telegrind.scheduler import Scheduler
    from telegrind.voice import VoiceJobs

    layout = Layout(config.data_dir)
    layout.install_contract()  # REQ-LAYOUT-2
    bot = Bot(token=config.bot_token)
    provider = make_provider(config)
    engine_cm = Engine(config.data_dir / "state", max_concurrent=config.max_concurrent) if provider else None
    engine = await engine_cm.__aenter__() if engine_cm else None
    client = httpx.AsyncClient()
    try:
        app = App(config=config, bot=bot, layout=layout)
        agent = Agent(config=config, app=app, engine=engine, provider=provider)
        app.answerer = agent
        app.media = MediaJobs(app)
        app.voice = VoiceJobs(app, agent=agent, client=client)
        scheduler = Scheduler(app, agent)
        app.watcher = scheduler
        stranded = await agent.recover()
        log.info(
            "start: %d allowed users, %d stranded turns forgotten, agent %s",
            len(config.allowed_user_ids),
            stranded,
            "on" if provider else "off",
        )
        app.media.resume()
        app.voice.resume()
        ticker = asyncio.get_running_loop().create_task(scheduler.run(), name="scheduler")
        try:
            # handle_signals=False: aiogram's own handler would replace _exit_on_sigterm's with a graceful stop and
            # a drain that can outlast the container's grace period (REQ-TURN-9: exit, keep the markers).
            await build_dispatcher(app).start_polling(
                bot, allowed_updates=["message", "edited_message", "message_reaction"], handle_signals=False
            )
        finally:
            ticker.cancel()
            await app.drain()
            app.close()
    finally:
        await client.aclose()
        await bot.session.close()
        if engine_cm:
            await engine_cm.__aexit__(None, None, None)


def _exit_on_sigterm() -> None:
    """PID 1 in the container ignores a signal with no handler; exiting keeps televibe's markers for recover()."""

    def stop(*_: object) -> None:
        log.warning("SIGTERM: exiting; turns in flight are recovered on the next start")
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegrind")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the bot")
    sub.add_parser("doctor", help="check binaries, ownership and per-person counts")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env(os.environ)
    except ConfigError as exc:
        print(f"telegrind: {exc}", file=sys.stderr)  # noqa: T201 - the CLI's own error line
        return 2
    if args.command == "doctor":
        return doctor(config, sys.stdout)
    _exit_on_sigterm()
    asyncio.run(run(config))
    return 0
