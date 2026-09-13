import asyncio
import json
import signal
import uuid
from typing import Any

import pytest

from telegrind.meta import runtime
from telegrind.meta.config import MetaConfig

CFG = MetaConfig(admin_chat_ids=frozenset({7}), binary="/usr/bin/claude", cwd="/app")
NEW = uuid.UUID("11111111-1111-5111-8111-111111111111")
BASE = uuid.UUID("22222222-2222-5222-8222-222222222222")


def test_a_fresh_conversation_names_its_own_session() -> None:
    argv = runtime.argv(CFG, "привет", session_id=NEW, resume_from=None)
    assert argv[0] == "/usr/bin/claude"
    assert "--session-id" in argv and str(NEW) in argv
    assert "--resume" not in argv
    assert argv[-1] == "привет"


def test_a_continued_turn_forks_off_the_parent() -> None:
    """Resume-in-place breaks at the third turn: the two-hop rule resolves
    to a message whose session was never created. Forking is what makes the
    derived id self-consistent."""
    argv = runtime.argv(CFG, "а почему", session_id=NEW, resume_from=BASE)
    assert argv[argv.index("--resume") + 1] == str(BASE)
    assert "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == str(NEW)


def test_permissions_are_bypassed_and_nobody_answers_prompts() -> None:
    """Without --permission-prompts none, anything that would have prompted
    is silently denied instead."""
    argv = runtime.argv(CFG, "x", session_id=NEW, resume_from=None)
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--permission-prompts") + 1] == "none"


def spawning(payload: dict[str, Any], code: int = 0, stderr: str = "") -> Any:
    async def spawn(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
        return code, json.dumps(payload), stderr

    return spawn


async def test_a_successful_turn_returns_its_text() -> None:
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({"result": "Записал.", "is_error": False}),
    )
    assert result == runtime.TurnResult(text="Записал.", ok=True)


async def test_a_nonzero_exit_is_said_out_loud() -> None:
    """A silence the user cannot tell from thinking is worse than an error
    message — 👀 is a promise."""
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({}, code=1, stderr="boom"),
    )
    assert result.ok is False
    assert "boom" in result.text


async def test_a_timeout_is_said_out_loud() -> None:
    async def hanging(
        argv: list[str], cwd: str, timeout: float
    ) -> tuple[int, str, str]:
        raise TimeoutError

    result = await runtime.run_turn(CFG, "x", session_id=NEW, spawn=hanging)
    assert result.ok is False
    assert "не уложился" in result.text.lower()


async def test_unparseable_output_is_a_failure_not_a_reply() -> None:
    async def garbage(
        argv: list[str], cwd: str, timeout: float
    ) -> tuple[int, str, str]:
        return 0, "not json", ""

    result = await runtime.run_turn(CFG, "x", session_id=NEW, spawn=garbage)
    assert result.ok is False


async def test_a_missing_binary_is_said_out_loud() -> None:
    """`OSError` (ENOENT and friends) is a real, if rare, way a spawn can
    fail before there is any exit code or output to read at all."""

    async def missing(
        argv: list[str], cwd: str, timeout: float
    ) -> tuple[int, str, str]:
        raise FileNotFoundError("No such file or directory: 'claude'")

    result = await runtime.run_turn(CFG, "x", session_id=NEW, spawn=missing)
    assert result.ok is False
    assert "claude" in result.text


async def test_is_error_true_is_a_failure_even_with_exit_zero() -> None:
    """The CLI can exit 0 and still report the turn itself as failed —
    `is_error` is what actually says whether the turn succeeded."""
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({"result": "не смог найти факт.", "is_error": True}),
    )
    assert result.ok is False
    assert "не смог найти факт" in result.text


def test_the_prompt_is_separated_from_flags_with_a_double_dash() -> None:
    """A prompt beginning with `-` would otherwise be parsed as a flag —
    measured 2026-09-12: `--version` as the trailing positional printed the
    CLI's own version instead of reaching the model. `--` must sit
    immediately before the prompt in both the fresh-session and the
    resume-and-fork shapes."""
    fresh = runtime.argv(CFG, "-x", session_id=NEW, resume_from=None)
    assert fresh[-1] == "-x"
    assert fresh[-2] == "--"

    resumed = runtime.argv(CFG, "-x", session_id=NEW, resume_from=BASE)
    assert resumed[-1] == "-x"
    assert resumed[-2] == "--"


async def test_missing_result_is_a_failure_not_a_silent_success() -> None:
    """`is_error` falsy with `result` absent must not read as a success with
    nothing to say — that is a promise (👀) broken silently. The text must be
    its own message, not a fallthrough to the `is_error` branch's "Ошибка."."""
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({"is_error": False}),
    )
    assert result.ok is False
    assert result.text not in ("", "Ошибка.")


async def test_null_result_is_a_failure_not_a_silent_success() -> None:
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({"result": None, "is_error": False}),
    )
    assert result.ok is False
    assert result.text not in ("", "Ошибка.")


async def test_empty_string_result_is_a_failure_not_a_silent_success() -> None:
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({"result": "", "is_error": False}),
    )
    assert result.ok is False
    assert result.text not in ("", "Ошибка.")


async def test_is_error_wins_over_an_empty_result() -> None:
    """The two failure branches are order-dependent: a failed turn that also
    said nothing must still be reported as the CLI's own error, not as an
    empty answer. Nothing else in the suite pins that order — every other
    `is_error: True` fixture carries a non-empty `result`."""
    result = await runtime.run_turn(
        CFG,
        "x",
        session_id=NEW,
        spawn=spawning({"is_error": True}),
    )
    assert result.ok is False
    assert result.text == "Ошибка."


async def test_a_non_dict_payload_is_a_failure_not_a_crash() -> None:
    """`json.loads` can succeed on a bare list, `null`, or a string — none of
    those support `.get`, so this must not raise `AttributeError` out of a
    function whose docstring promises it never raises."""
    for body in ("[1,2,3]", "null", '"x"'):

        async def spawn(
            argv: list[str], cwd: str, timeout: float, body: str = body
        ) -> tuple[int, str, str]:
            return 0, body, ""

        result = await runtime.run_turn(CFG, "x", session_id=NEW, spawn=spawn)
        assert result.ok is False


class FakeProc:
    """Enough of an `asyncio.subprocess.Process` for `_spawn`.

    `communicate` is a coroutine that either answers or never returns; the
    second is how a hung turn is reproduced without hanging a test, because
    `wait_for` cancels it on the timeout.
    """

    def __init__(self, *, hangs: bool = False, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = 0
        self._hangs = hangs
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hangs:
            await asyncio.Event().wait()
        return b'{"result": "ok"}', b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def spawning_fake(monkeypatch: Any, proc: FakeProc) -> dict[str, Any]:
    """Stand in for `asyncio.create_subprocess_exec` and record the kwargs.

    A fake, not a subprocess: nothing here starts a process, so the
    no-subprocess rule holds.
    """
    seen: dict[str, Any] = {}

    async def create(*argv: str, **kwargs: Any) -> FakeProc:
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return seen


async def test_a_turn_never_inherits_the_terminal(monkeypatch: Any) -> None:
    """A bot started from a shell hands its TTY to every turn otherwise.

    `claude -p` reading the operator's keyboard is not a mode anyone asked
    for, and in the systemd case stdin is whatever the unit happened to
    get.
    """
    seen = spawning_fake(monkeypatch, FakeProc())

    await runtime._spawn(["/usr/bin/claude", "-p"], "/app", 1.0)

    assert seen["stdin"] is asyncio.subprocess.DEVNULL


async def test_a_timed_out_turn_kills_the_whole_process_group(
    monkeypatch: Any,
) -> None:
    """`claude` is a node process that spawns its own tools and MCP
    servers. `proc.kill()` signals the direct child only, so those outlive
    the timeout — and the bot goes on running beside them.

    `start_new_session=True` is what makes a group to kill; without it the
    group is the bot's own, and killing it would kill the bot.
    """
    proc = FakeProc(hangs=True, pid=4242)
    seen = spawning_fake(monkeypatch, proc)
    killed: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))

    monkeypatch.setattr(runtime.os, "killpg", killpg)

    with pytest.raises(TimeoutError):
        await runtime._spawn(["/usr/bin/claude", "-p"], "/app", 0.01)

    assert seen["start_new_session"] is True
    assert killed == [(4242, signal.SIGKILL)]


async def test_a_group_that_is_already_gone_is_not_an_error(
    monkeypatch: Any,
) -> None:
    """The race is real: the process can exit between the timeout firing
    and the signal. A ProcessLookupError here would replace the timeout —
    which `run_turn` handles and says out loud — with an exception that
    reaches `queue._turn`'s catch-all as «что-то сломалось»."""
    proc = FakeProc(hangs=True)
    spawning_fake(monkeypatch, proc)

    def gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(runtime.os, "killpg", gone)

    with pytest.raises(TimeoutError):
        await runtime._spawn(["/usr/bin/claude", "-p"], "/app", 0.01)
