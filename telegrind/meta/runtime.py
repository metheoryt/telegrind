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
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegrind import llm
from telegrind.meta.config import MetaConfig

log = logging.getLogger(__name__)

Spawn = Callable[[list[str], str, float], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    text: str
    ok: bool


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
        llm.META_SYSTEM,
    ]
    if resume_from is not None:
        line += ["--resume", str(resume_from), "--fork-session"]
    line += ["--session-id", str(session_id), prompt]
    return line


async def _spawn(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(), err.decode()


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
        )
    except OSError as exc:
        log.warning("could not start %s: %s", cfg.binary, exc)
        return TurnResult(text=f"Не смог запуститься: {exc}", ok=False)

    if code != 0:
        # Measured 2026-09-12: a --resume naming a session that was never
        # written (the merged-batch case the session design warns about)
        # fails here, before any model call — exit 1, stdout empty, stderr
        # "No conversation found with session ID: <uuid>". That sentence
        # lands verbatim in the text below, which is what makes a failed
        # resume distinguishable from any other crash: the caller can match
        # on "No conversation found" to retry with no resume, per the
        # session design's own escape hatch.
        log.warning("turn %s exited %s: %s", session_id, code, err.strip())
        return TurnResult(text=f"Упал с кодом {code}: {err.strip()[:400]}", ok=False)

    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        log.warning("turn %s answered with something that is not JSON", session_id)
        return TurnResult(text="Ответ пришёл в нечитаемом виде.", ok=False)

    if payload.get("is_error"):
        return TurnResult(text=str(payload.get("result") or "Ошибка."), ok=False)

    return TurnResult(text=str(payload.get("result") or ""), ok=True)
