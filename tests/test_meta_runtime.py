import json
import uuid
from typing import Any

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
