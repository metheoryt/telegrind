"""One `claude -p` per turn, forked from the session it is answering.

This is full Claude Code, not a trimmed one: the same CLAUDE.md, skills,
hooks and MCP servers the user has in the TUI. The fork is what keeps the
derived session id self-consistent — a turn is named by the user message
that caused it, and every turn writes to its own id, so two replies to
genuinely different points cannot interleave in one transcript.

Measured 2026-09-12 on this box (claude 2.1.269): `--output-format json`
returns one object carrying `result`, `is_error` and `session_id`.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegrind.meta.config import MetaConfig

log = logging.getLogger(__name__)

#: Appended to every turn, and it says one thing: this is a Telegram chat,
#: not an essay.
#:
#: It lives here rather than in the host's `llm.py`, which is where it was
#: written. `llm.py` is the Anthropic-API module — its client, its model
#: names, its two call helpers — and this prompt goes to none of them; it
#: is argv for a subprocess. Keeping it there was also the one breach of
#: the guest boundary this package's docstring denies, and the reason
#: «separating the module later is deleting one wiring file» was not true.
#: Not a `MetaConfig` field either: `MetaConfig` is read from the
#: environment, and a prompt is not something an operator sets.
META_SYSTEM = """\
Ты отвечаешь в личном чате в Telegram. Это разговор, а не эссе.

- Говори коротко: одна-две фразы, разворачивай только если попросили.
- Никаких стен текста — их никто не читает с телефона.
- Если ответ не помещается в разговор (отчёт, таблица, дифф) — скажи об
  этом и предложи выгрузить файлом, а не дроби на три сообщения.
- Жёсткий предел одного сообщения — 4096 символов.
"""

Spawn = Callable[[list[str], str, float], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    text: str
    ok: bool
    #: The process's exit status, when a process actually finished. `None`
    #: means none did — it could not start, or it was killed on the timeout.
    #: A fact rather than a policy: it is the only thing that separates «the
    #: CLI refused before the turn began» from «the turn ran and went
    #: wrong», and `queue.py` needs that separation to decide whether a cold
    #: retry could possibly help.
    exit_code: int | None = 0


def argv(
    cfg: MetaConfig,
    prompt: str,
    *,
    session_id: uuid.UUID,
    resume_from: uuid.UUID | None,
) -> list[str]:
    """The command line for one turn.

    A fresh conversation names its own session. A continued one forks off
    the parent's: `--resume <base> --fork-session --session-id <new>` keeps
    the history and writes it under the id we name.
    """
    line = [
        cfg.binary,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        # Without this, anything that would have prompted is silently
        # denied instead of being answered.
        "--permission-prompts",
        "none",
        "--append-system-prompt",
        META_SYSTEM,
    ]
    if resume_from is not None:
        line += ["--resume", str(resume_from), "--fork-session"]
    # `--` is not decoration. The prompt is whatever the user typed, and
    # without the separator the CLI parses a message beginning with a dash
    # as its own flags: measured 2026-09-12 against claude 2.1.269, a
    # prompt of `--version` printed the version and never reached the
    # model. That is argv injection into a bypassPermissions subprocess.
    line += ["--session-id", str(session_id), "--", prompt]
    return line


async def _spawn(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    """One process per turn, in a session of its own, deaf to the terminal.

    `stdin=DEVNULL`, because the bot is started by hand from a shell on
    dev: without it every turn inherits that TTY, and `claude -p` reading
    the operator's keyboard is not a mode anyone asked for.

    `start_new_session=True` is the other half of the timeout. `claude` is
    a node process that spawns its own tools and MCP servers, and
    `proc.kill()` signals the direct child only — the children outlive the
    turn that was given up on, and the bot goes on running beside them.
    The new session makes a process group whose id is the child's pid, so
    `killpg` reaches all of it. Without it the group is the *bot's* own,
    and killing the group would kill the bot.

    Not verified end to end: no test may start a process, so the argument
    is asserted against a fake `create_subprocess_exec` and the orphan
    survival itself is walk item 18's to confirm.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # Suppressed, not ignored: the process can exit between the timeout
        # firing and the signal, and a ProcessLookupError here would
        # replace the TimeoutError — which `run_turn` handles and says out
        # loud — with a crash string.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def run_turn(
    cfg: MetaConfig,
    prompt: str,
    *,
    session_id: uuid.UUID,
    resume_from: uuid.UUID | None = None,
    spawn: Spawn = _spawn,
) -> TurnResult:
    """Run one turn. Never raises: 👀 is a promise, and a crashed or hung
    process breaks it silently unless the failure is said out loud."""
    line = argv(cfg, prompt, session_id=session_id, resume_from=resume_from)
    try:
        code, out, err = await spawn(line, cfg.cwd, cfg.timeout)
    except TimeoutError:
        log.warning("turn %s did not finish in %ss", session_id, cfg.timeout)
        return TurnResult(
            text=f"Не уложился в {int(cfg.timeout)} секунд — попробуй ещё раз.",
            ok=False,
            exit_code=None,
        )
    except OSError as exc:
        log.warning("could not start %s: %s", cfg.binary, exc)
        return TurnResult(text=f"Не смог запуститься: {exc}", ok=False, exit_code=None)

    if code != 0:
        # Measured 2026-09-12: a --resume naming a session that was never
        # written (the merged-batch case the session design warns about)
        # fails here, before any model call — exit 1, stdout empty, stderr
        # "No conversation found with session ID: <uuid>". That sentence lands
        # verbatim in the text below, which is useful evidence, but it is
        # truncated to 400 characters, so it is not a guaranteed substring.
        # The caller should branch on `ok` plus "did I pass a resume", not on
        # this wording — that is robust against the CLI changing its message.
        log.warning("turn %s exited %s: %s", session_id, code, err.strip())
        return TurnResult(
            text=f"Упал с кодом {code}: {err.strip()[:400]}", ok=False, exit_code=code
        )

    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        log.warning("turn %s answered with something that is not JSON", session_id)
        return TurnResult(text="Ответ пришёл в нечитаемом виде.", ok=False)

    if not isinstance(payload, dict):
        log.warning("turn %s answered with JSON that is not an object", session_id)
        return TurnResult(text="Ответ пришёл в нечитаемом виде.", ok=False)

    if payload.get("is_error"):
        return TurnResult(text=str(payload.get("result") or "Ошибка."), ok=False)

    result_text = payload.get("result")
    if not result_text:
        log.warning("turn %s returned no result text", session_id)
        return TurnResult(text="Ответ пришёл пустым.", ok=False)

    return TurnResult(text=str(result_text), ok=True)
