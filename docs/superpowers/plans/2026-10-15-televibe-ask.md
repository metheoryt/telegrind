# televibe 0.4 — One-Shot Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give televibe a sessionless, tool-less `claude -p` call that answers in a JSON schema, so bots stop hand-rolling it (jared's `cleanup.py`) and telegrind v3 can clean voice transcripts through the library.

**Architecture:** A new core module `televibe/oneshot.py` runs one `claude -p --output-format stream-json --json-schema … --tools "" --no-session-persistence` process in a throw-away directory, with the same environment rules, process-group kill, stdin/stderr handling and parser as a turn, but outside the `Engine`: no slot, no lane, no session lock, no marker. The result is an `Answer(data, usage, limits)`; any failure is an `AskFailed(TelevibeError)` carrying a `FailReason`. `ClaudeCode.ask()` is the entry point; `Codex.ask()` refuses.

**Tech Stack:** Python 3.12+, asyncio, no runtime dependencies (REQ-SCOPE-2). Tests: pytest + the existing fake `claude` binary replaying recorded output. Tooling: `uv run pytest`, `uvx ruff check src tests`, `uvx ty check`.

**Spec:** `~/my/televibe/SPEC.md`. This plan adds a section to it in Task 1; every later task argues from that section. The plan itself lives in the telegrind repository (`docs/superpowers/plans/`), not in televibe: televibe is public and keeps no plans or decision docs in its history.

**Where the work happens:** a `us:worktrees` workspace of `~/my/televibe`, never the main checkout:

```bash
cd ~/my/televibe && bash ~/my/us/plugins/us/lib/wt.sh create --kind work --key ask --name 'televibe 0.4 one-shot calls' --base main --branch work/ask
```

All paths below are relative to that workspace. At the end the branch is fast-forwarded into `main` from the main checkout and pushed; the `v0.4.0` tag (which publishes to PyPI) is pushed only on Maxim's explicit yes.

## Global Constraints

- REQ-SCOPE-2: `dependencies = []` in `pyproject.toml` stays empty; nothing new is imported but the standard library.
- REQ-SCOPE-1: no module under `src/televibe/` outside `televibe/telegram/` imports a chat client. `oneshot.py` imports none.
- REQ-API-1: `televibe.__all__` is pinned by `tests/test_api.py`; adding `Answer` and `AskFailed` means editing that test in the same task.
- REQ-API-2: every misuse is a `TelevibeError` raised before a process starts; nothing is silently adjusted.
- REQ-ENV-1..4: the child environment is built by `televibe.env.child_env` from the allowlist, API keys and account variables are refused in `env`.
- REQ-RUN-1/2: prompt on stdin then stdin closed; stderr drained concurrently.
- REQ-TEST-1: every requirement id in `SPEC.md` appears in a test name or docstring (`tests/test_requirements.py` enforces it; a new REQ with no test fails the build).
- REQ-TEST-3: recorded outputs live in `tests/fixtures/claude/`, sanitized with `tests/fixtures/sanitize.py`, listed in `tests/fixtures/README.md` with the CLI version.
- Style: `ruff` rules `E, F, I, UP, B, ANN` (ANN off in tests), `ty` clean for library code; existing code uses `from __future__ import annotations`, frozen slotted dataclasses, and `TelevibeError` messages that say what to do instead.
- Commits: imperative subject, no `Co-Authored-By` trailer. Release commit subject `Release 0.4.0: <what>`.

## Review Focus

Inputs the spec implies but no task's test exercises until the lines below add them:

1. **A `--json-schema` the CLI rejects** (not valid JSON Schema): the CLI exits non-zero with a message on stderr and no `result` record. Expected: `AskFailed(provider_error)` with that stderr in `detail`, not a hang and not a `TelevibeError` about misuse. Test added to Task 4 (`test_ask_exits_without_result_is_provider_error`, via `FAKE_EXIT=2` plus `FAKE_STDERR`).
2. **A prompt holding a lone surrogate**: `engine.turn` refuses it (encode check); `ask` must too, before spawning. Test added to Task 3 (`test_ask_refuses_unencodable_prompt`).
3. **A schema with non-ASCII text** (Russian descriptions): it must reach the CLI intact in argv. Test added to Task 4 (`test_ask_passes_the_schema_compact_and_utf8`, reads the fake's recorded argv back and `json.loads` it).
4. **Cancellation while the process is being spawned**: the task awaiting `ask()` is cancelled between `create_subprocess_exec` returning and the first read. Expected: the process group is dead afterwards and `CancelledError` propagates. Test added to Task 4 (`test_ask_cancel_kills_the_process`, cancels right after the record file appears).
5. **A second `result`-less line after the result** (the CLI keeps writing after `result`): the reader stops at the terminal record and the process gets `EXIT_GRACE_S` to exit, then is killed. Covered by reusing the turn's reader logic; pinned in Task 4 by `test_ask_waits_for_exit_then_kills` with `FAKE_MODE=orphan`.

---

### Task 1: Specify one-shot calls in SPEC.md

**Files:**
- Modify: `SPEC.md` (new section before "The Telegram layer"; renumber the sections after it; new line in "Not measured yet"; API surface line)
- Modify: `README.md` (one paragraph; section reference)
- Modify: `src/televibe/providers/claude.py:62` (comment "(section 13)" → "(section 14)")
- Modify: `src/televibe/telegram/__init__.py:1`, `src/televibe/telegram/chains.py:1`, `src/televibe/telegram/store.py:1` (docstrings "section 12" → "section 13")

**Interfaces:**
- Produces: the requirement ids REQ-ASK-1..6 that every later task's test docstring names.

- [ ] **Step 1: Insert the new section**

Find the line `## 12. The Telegram layer` in `SPEC.md`. Insert the following block immediately before it (the `---` separator before "## 12." stays above the new section):

````markdown
## 12. One-shot calls

Some work needs a model and nothing else: turning a speech transcript into
clean text, deciding what kind of message something is, pulling three fields
out of a document. A turn is the wrong tool for it. A turn opens a session
and keeps a transcript, waits in the engine's queue behind real turns, and
may use tools. A one-shot call is `claude -p` with no tools, no session, no
MCP servers and a JSON schema for the answer, on the same account as the
turns, outside the engine.

```python
answer = await claude.ask(
    "Говорит Максим.\n\n<расшифровка>…</расшифровка>",
    schema={"type": "object", "required": ["text"],
            "properties": {"text": {"type": "string"}}},
    system="Ты чистишь расшифровку голосового сообщения…",
    model="sonnet",
    timeout_s=180,
)
answer.data["text"]
```

### ASK

- **REQ-ASK-1** — `provider.ask(prompt, *, schema, system=None, model=None,
  env={}, timeout_s=180)` runs one call and returns `Answer(data, usage,
  limits)`: `data` is the agent's structured output, the JSON value it
  produced against `schema`; `usage` is the agent's token counts as in
  REQ-TURN-9; `limits` is the last `Limits` the call reported, or None.
  Claude Code only in this version. `Codex.ask` raises `TelevibeError`
  (section 14).
- **REQ-ASK-2** — The command is `claude -p --output-format stream-json
  --verbose --json-schema <schema> --tools "" --strict-mcp-config
  --no-session-persistence`, plus `--model <model>` and `--system-prompt
  <system>` when given. `<schema>` is the schema as compact JSON
  (`separators=(",", ":")`, `ensure_ascii=False`). `--system-prompt`
  replaces the agent's default system prompt, where a turn's
  `instructions` only appends to it (REQ-CLAUDE-1). No `--permission-mode`:
  with no tools there is nothing to permit.
- **REQ-ASK-3** — The call runs in an empty temporary directory of its own,
  removed when the call ends, so no `CLAUDE.md` on any path reaches it. Its
  environment is a turn's (REQ-ENV-1 to REQ-ENV-4, the same refusals). Its
  process is its own group, killed on timeout and when the task awaiting the
  call is cancelled. The prompt goes to stdin, which is then closed
  (REQ-RUN-1); stderr is read concurrently (REQ-RUN-2); after the `result`
  record the process gets the same grace as a turn, then is killed
  (REQ-RUN-7).
- **REQ-ASK-4** — A call that does not return an answer raises `AskFailed`,
  a `TelevibeError` with `reason: FailReason` and `detail: str`. The reasons
  are a turn's: a `result` with `is_error` is `provider_error`, or
  `usage_limit` when the call was refused at the limit (REQ-CLAUDE-3); a
  process that cannot be started is `spawn_error`; a process that exits
  without a `result` is `provider_error` with the stderr tail or the exit
  code (REQ-RUN-5); a call that runs past `timeout_s` is `timeout`; a
  cancelled call does not raise `AskFailed` — the `CancelledError` is the
  caller's own. A `result` without `structured_output` is `provider_error`
  saying the agent produced no structured output.
- **REQ-ASK-5** — Misuse raises `TelevibeError` before any process starts
  (REQ-API-2): an empty or non-UTF-8 prompt, a `schema` that is not a
  JSON-serializable mapping, a `system` or `model` that is not a str, a
  forbidden variable in `env`, a `timeout_s` that is not a positive finite
  number.
- **REQ-ASK-6** — The call writes no marker and takes no slot, lane or
  session lock: `engine.stranded()` never reports it, and it never waits
  behind a turn. A caller who needs to bound concurrent calls does it with a
  semaphore of their own; televibe does not.

---

````

- [ ] **Step 2: Renumber the sections after it**

In `SPEC.md`, change the headings `## 12. The Telegram layer` → `## 13. The Telegram layer`, `## 13. Not measured yet` → `## 14. Not measured yet`, `## 14. Out of scope for v1` → `## 15. Out of scope for v1`, `## 15. Tests` → `## 16. Tests`. Then update every in-text reference:

```bash
grep -n "section 1[2-5]" SPEC.md README.md src tests
```

Each hit that pointed at the Telegram layer now says `section 13`; "Not measured yet" → `section 14`; "Out of scope" → `section 15`. Known hits: `SPEC.md` section 1 ("An optional layer, `televibe.telegram` (section 12)"), REQ-TEST-2 ("The Telegram layer's tests (section 12)"), section 15's "In the Telegram layer" bullet does not cite a number; `src/televibe/providers/claude.py` `_limits` docstring "(section 13)" → "(section 14)"; `src/televibe/telegram/__init__.py`, `chains.py`, `store.py` docstrings "(section 12 …)" → "(section 13 …)"; `README.md` "See SPEC.md section 12" → "section 13". Re-run the grep until every hit is right.

- [ ] **Step 3: Update REQ-API-1 and the "Not measured yet" list**

In REQ-API-1 change the surface list to: `Engine`, `Turn`, `Session`, `Account`, `Access`, `ClaudeCode`, `Codex`, `FailReason`, `Stranded`, the event classes of section 5 with `LimitWindow`, `Answer`, and `TelevibeError` with its subclasses (`AskFailed`).

Append to "## 14. Not measured yet":

```markdown
- **A one-shot call on Codex.** `codex exec --output-schema <file>` exists,
  but `codex exec --help` (codex-cli 0.161.0) shows no flag that runs it with no
  tools, and `--output-schema` has not been recorded. `Codex.ask` refuses until it is (REQ-ASK-1).
- **Whether `--output-format stream-json` carries `structured_output` on the
  `result` record exactly as `--output-format json` does.** The recording
  `claude/ask.jsonl` (Task 2) is the measurement; if it had been absent,
  REQ-ASK-2 would have said `--output-format json` and the reader would parse
  the whole stdout as one document.
```

(The second bullet is written in the past tense on purpose: Task 2 either confirms it and the bullet stays as a record, or refutes it and Task 2 rewrites REQ-ASK-2 before any code is written.)

- [ ] **Step 4: README paragraph**

After the paragraph that ends "televibe never sends a message anywhere." add:

```markdown
A one-shot call answers in a JSON schema with no session, no tools and no
place in the engine's queue: `await claude.ask(prompt, schema={...},
system="...")` returns `Answer(data, usage, limits)` or raises `AskFailed`
with the same reasons a turn fails for. It is for the model-only chores a bot
has around its turns — cleaning a transcript, classifying a message. See
SPEC.md section 12.
```

- [ ] **Step 5: Run the requirements test and watch it fail for the right reason**

Run: `uv run pytest tests/test_requirements.py -v`
Expected: FAIL — `missing == ['REQ-ASK-1', 'REQ-ASK-2', 'REQ-ASK-3', 'REQ-ASK-4', 'REQ-ASK-5', 'REQ-ASK-6']`. Nothing else in the suite is touched; `uv run pytest -q --deselect tests/test_requirements.py::test_every_requirement_is_claimed_by_a_test` stays green.

- [ ] **Step 6: Commit**

```bash
git add SPEC.md README.md src/televibe/providers/claude.py src/televibe/telegram/__init__.py src/televibe/telegram/chains.py src/televibe/telegram/store.py
git commit -m "SPEC: one-shot calls — provider.ask() answers in a JSON schema outside the engine"
```

---

### Task 2: Record the fixture `claude/ask.jsonl`

**Files:**
- Create: `tests/fixtures/claude/ask.jsonl`
- Modify: `tests/fixtures/README.md` (one table row)
- Possibly modify: `SPEC.md` REQ-ASK-2 (only if the measurement refutes stream-json)

**Interfaces:**
- Produces: a one-line (or few-line) JSONL recording whose `result` record has `"structured_output": {"word": "ok"}`, `"usage": {...}`, `"is_error": false`. Tasks 4 and 5 replay it.

- [ ] **Step 1: Record a real run on the subscription**

The recording needs a logged-in Claude Code account directory. Use the one the live tests use (`TELEVIBE_LIVE_CLAUDE_HOME`, e.g. `~/.claude-televibe`); never `--bare` (it ignores OAuth). Run from an empty directory so no `CLAUDE.md` is picked up:

```bash
mkdir -p .probes/ask && cd .probes/ask
CLAUDE_CONFIG_DIR="$HOME/.claude-televibe" claude -p --output-format stream-json --verbose \
  --json-schema '{"type":"object","additionalProperties":false,"required":["word"],"properties":{"word":{"type":"string"}}}' \
  --tools "" --strict-mcp-config --no-session-persistence --model sonnet \
  --system-prompt 'Answer with one English word in the field word.' \
  <<< 'Which word do people say to agree? Answer with that word only.' > raw.jsonl 2> raw.stderr
echo "exit $?"; wc -l raw.jsonl; grep -c structured_output raw.jsonl
```

Expected: exit 0, several lines (`system/init`, `assistant`, `result`, possibly `rate_limit_event`), and `grep -c` prints `1`: the `result` line carries `structured_output`. `.probes/` is gitignored.

If `grep -c` prints `0`: stop, rerun with `--output-format json` (one JSON document, possibly pretty-printed over many lines), and rewrite REQ-ASK-2 to say `--output-format json`, with the reader parsing the whole stdout as one JSON document (the "Not measured" bullet already describes this branch). Then Tasks 4 and 5 use `json.loads(stdout)` instead of `json_records`. Record which branch was taken in the commit message.

- [ ] **Step 2: Sanitize and store**

```bash
cd ../.. && uv run python tests/fixtures/sanitize.py .probes/ask/raw.jsonl > tests/fixtures/claude/ask.jsonl
```

Open the file and check: the session id is `{{SESSION_ID}}`, paths are `{{PATH}}`, no account hooks/tools/plugins remain, `structured_output` is intact, and the `result` record's `usage` is present. If `sanitize.py` chokes on a field it has not seen (a `structured_output` key is new), extend it in the same way it handles `result` (it is a small script; the key is passed through untouched).

- [ ] **Step 3: Note the recording in the fixtures README**

Add a row to the table in `tests/fixtures/README.md`:

```markdown
| `claude/ask.jsonl` | Claude Code 2.1.300 | One-shot call: `--json-schema`, `--tools ""`, `--no-session-persistence`, `--system-prompt`; the `result` carries `structured_output: {"word": "ok"}`. No tool use. |
```

Replace `2.1.300` with `claude --version` of the recording box.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/claude/ask.jsonl tests/fixtures/README.md
git commit -m "fixtures: record a one-shot call with --json-schema (claude/ask.jsonl)"
```

---

### Task 3: `Answer`, `AskFailed`, argument checks and the public surface

**Files:**
- Create: `src/televibe/oneshot.py` (dataclass, exception, `check_ask_args`; the runner comes in Task 4)
- Modify: `src/televibe/providers/base.py` (`Provider.ask` default refusing)
- Modify: `src/televibe/providers/claude.py` (`ClaudeCode.ask` validating, `ask_argv`; no process yet)
- Modify: `src/televibe/__init__.py`, `tests/test_api.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Produces:
  - `televibe.oneshot.Answer(data: Any, usage: dict[str, Any], limits: Limits | None)` — frozen, slotted.
  - `televibe.oneshot.AskFailed(reason: FailReason, detail: str)` — `TelevibeError` subclass with those two attributes; `str()` is `f"{reason.value}: {detail}"`.
  - `televibe.oneshot.AskSpec(prompt: str, schema_json: str, system: str | None, model: str | None, env: dict[str, str], timeout_s: float)` — frozen, slotted; `env` has `repr=False`.
  - `televibe.oneshot.check_ask_args(prompt, schema, system, model, env, timeout_s) -> AskSpec` — raises `TelevibeError` on misuse.
  - `ClaudeCode.ask_argv(spec: AskSpec) -> list[str]`.
  - `Provider.ask(...)` async, raising `TelevibeError(f"{kind} has no one-shot call in this version (REQ-ASK-1)")` unless overridden.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ask.py`:

```python
"""One-shot calls (SPEC section 12): arguments, argv, surface. Process tests are in test_ask_run.py."""

import json

import pytest

from televibe import Account, Answer, AskFailed, ClaudeCode, Codex, FailReason, TelevibeError
from televibe.oneshot import AskSpec, check_ask_args

SCHEMA = {"type": "object", "required": ["word"], "properties": {"word": {"type": "string", "description": "одно слово"}}}


@pytest.fixture
def claude(tmp_path):
    return ClaudeCode(Account(tmp_path / "acc"))


def test_answer_and_askfailed_shapes():
    """REQ-ASK-1, REQ-ASK-4: Answer carries data, usage, limits; AskFailed is a TelevibeError with reason and detail."""
    answer = Answer({"word": "ok"}, {"input_tokens": 1}, None)
    assert answer.data == {"word": "ok"} and answer.usage == {"input_tokens": 1} and answer.limits is None
    failed = AskFailed(FailReason.TIMEOUT, "no result within 3 s")
    assert isinstance(failed, TelevibeError)
    assert failed.reason is FailReason.TIMEOUT and failed.detail == "no result within 3 s"
    assert str(failed) == "timeout: no result within 3 s"


def test_check_ask_args_builds_a_spec():
    """REQ-ASK-2: the schema is compact JSON with non-ASCII kept; defaults are None, {}, 180."""
    spec = check_ask_args("hi", SCHEMA, None, None, {}, 180)
    assert isinstance(spec, AskSpec)
    assert spec.schema_json == json.dumps(SCHEMA, separators=(",", ":"), ensure_ascii=False)
    assert "одно слово" in spec.schema_json and " " not in spec.schema_json.replace("одно слово", "")
    assert spec.system is None and spec.model is None and spec.env == {} and spec.timeout_s == 180.0
    assert "FAKE" not in repr(AskSpec("p", "{}", None, None, {"FAKE_TOKEN": "x"}, 1.0))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": ""},
        {"prompt": "a\udc80b"},
        {"schema": "not a mapping"},
        {"schema": {"x": object()}},
        {"system": 5},
        {"model": 5},
        {"env": {"ANTHROPIC_API_KEY": "sk"}},
        {"env": {"CLAUDE_CONFIG_DIR": "/x"}},
        {"timeout_s": 0},
        {"timeout_s": float("inf")},
        {"timeout_s": True},
    ],
)
def test_check_ask_args_refuses_misuse(kwargs):
    """REQ-ASK-5, REQ-API-2: every misuse is a TelevibeError before any process."""
    args = {"prompt": "hi", "schema": SCHEMA, "system": None, "model": None, "env": {}, "timeout_s": 180, **kwargs}
    with pytest.raises(TelevibeError):
        check_ask_args(**args)


def test_ask_refuses_unencodable_prompt(claude):
    """REQ-ASK-5: a lone surrogate cannot reach stdin; refused at the call like engine.turn."""
    with pytest.raises(TelevibeError, match="UTF-8"):
        check_ask_args("a\udc80b", SCHEMA, None, None, {}, 180)


def test_ask_argv(claude):
    """REQ-ASK-2: the fixed flags, then --model and --system-prompt only when given."""
    spec = check_ask_args("hi", SCHEMA, None, None, {}, 180)
    assert claude.ask_argv(spec) == [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--json-schema", spec.schema_json, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
    ]
    spec = check_ask_args("hi", SCHEMA, "be terse", "sonnet", {}, 180)
    argv = claude.ask_argv(spec)
    assert argv[-4:] == ["--model", "sonnet", "--system-prompt", "be terse"]
    assert "--permission-mode" not in argv and "--session-id" not in argv and "--resume" not in argv


async def test_codex_has_no_one_shot_call(tmp_path):
    """REQ-ASK-1: Codex.ask raises TelevibeError until a recording exists (section 14)."""
    with pytest.raises(TelevibeError, match="codex"):
        await Codex(Account(tmp_path / "acc")).ask("hi", schema=SCHEMA)
```

Update `tests/test_api.py`'s pinned list: add `"Answer"` and `"AskFailed"`.

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/test_ask.py tests/test_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'Answer' from 'televibe'`.

- [ ] **Step 3: Implement `oneshot.py` (data, exception, checks)**

Create `src/televibe/oneshot.py`:

```python
"""One-shot calls: an answer in a JSON schema, with no session, no tools and no place in the queue (section 12)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from televibe.env import check_turn_env
from televibe.errors import TelevibeError
from televibe.events import FailReason, Limits

DEFAULT_TIMEOUT_S = 180.0


@dataclass(frozen=True, slots=True)
class Answer:
    """What a one-shot call returned (REQ-ASK-1)."""

    data: Any
    usage: dict[str, Any]
    limits: Limits | None


class AskFailed(TelevibeError):
    """A one-shot call that did not return an answer (REQ-ASK-4)."""

    def __init__(self, reason: FailReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AskSpec:
    prompt: str
    schema_json: str
    system: str | None
    model: str | None
    env: dict[str, str] = field(repr=False)  # may hold tokens (REQ-ENV-4)
    timeout_s: float = DEFAULT_TIMEOUT_S


def check_ask_args(
    prompt: str,
    schema: Mapping[str, Any],
    system: str | None,
    model: str | None,
    env: Mapping[str, str],
    timeout_s: float,
) -> AskSpec:
    """Every misuse raises TelevibeError before a process starts (REQ-ASK-5)."""
    if not isinstance(prompt, str) or not prompt:
        raise TelevibeError("prompt must be a non-empty str")
    try:
        prompt.encode()
    except UnicodeEncodeError:
        raise TelevibeError("prompt must be text that encodes to UTF-8 (it holds a lone surrogate)") from None
    if not isinstance(schema, Mapping):
        raise TelevibeError("schema must be a mapping holding a JSON Schema")
    try:
        schema_json = json.dumps(dict(schema), separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TelevibeError(f"schema must be JSON-serializable: {exc}") from None
    for name, value in (("system", system), ("model", model)):
        if value is not None and not isinstance(value, str):
            raise TelevibeError(f"{name} must be a str or None")
        if value is not None and "\0" in value:
            raise TelevibeError(f"{name} must not contain a NUL character")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise TelevibeError("timeout_s must be a positive, finite number of seconds")
    return AskSpec(prompt, schema_json, system, model, check_turn_env(env), float(timeout_s))
```

- [ ] **Step 4: Add `ask` to the providers**

In `src/televibe/providers/base.py`, add to the imports `from collections.abc import Mapping` and `from typing import Any` (already imported), `from televibe.oneshot import Answer`, and inside `class Provider` after `check_access`:

```python
    async def ask(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        system: str | None = None,
        model: str | None = None,
        env: Mapping[str, str] = MappingProxyType({}),
        timeout_s: float = 180,
    ) -> Answer:
        """One answer in `schema`, with no session and no tools (REQ-ASK-1). Claude Code only."""
        raise TelevibeError(f"{self.kind} has no one-shot call in this version (REQ-ASK-1)")
```

Add `from types import MappingProxyType` to that file's imports. Check for an import cycle: `oneshot.py` imports `televibe.env`, `televibe.errors`, `televibe.events` only, none of which import providers, so `base.py` importing `oneshot` is safe.

In `src/televibe/providers/claude.py`, add `from televibe.oneshot import AskSpec` and inside `class ClaudeCode`:

```python
    def ask_argv(self, spec: AskSpec) -> list[str]:
        """REQ-ASK-2: no tools, no session, no MCP, the schema as compact JSON."""
        argv = [
            self.binary, "-p", "--output-format", "stream-json", "--verbose",
            "--json-schema", spec.schema_json, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
        ]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.system:
            argv += ["--system-prompt", spec.system]
        return argv
```

(`ClaudeCode.ask` itself, which spawns the process, is Task 4. Until then `ClaudeCode(...).ask(...)` raises the base refusal; no test in this task calls it.)

In `src/televibe/__init__.py` add `from televibe.oneshot import Answer, AskFailed` and the two names to `__all__` (keep the list sorted as it is now: insert `"Answer"` after `"Account"`, `"AskFailed"` after `"Answer"`).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_ask.py tests/test_api.py -v`
Expected: PASS, 7 tests in `test_ask.py` plus the surface test.

Run: `uvx ruff check src tests && uvx ty check`
Expected: ruff clean; ty reports nothing new in `src/` (the baseline warnings from tests are known).

- [ ] **Step 6: Commit**

```bash
git add src/televibe/oneshot.py src/televibe/providers/base.py src/televibe/providers/claude.py src/televibe/__init__.py tests/test_ask.py tests/test_api.py
git commit -m "oneshot: Answer, AskFailed, argument checks and ask_argv"
```

---

### Task 4: Run the process: `ClaudeCode.ask()`

**Files:**
- Modify: `src/televibe/oneshot.py` (add `run_oneshot`)
- Modify: `src/televibe/providers/claude.py` (`_ClaudeParser.structured_output`; `ClaudeCode.ask`)
- Modify: `tests/fakes/fakeagent.py` (no change needed unless Step 1 shows the fake cannot replay `ask.jsonl`; see note)
- Test: `tests/test_ask_run.py`

**Interfaces:**
- Consumes: `AskSpec`, `check_ask_args`, `ClaudeCode.ask_argv` (Task 3); `televibe.turn._Tail`, `_feed_stdin`, `_leader_exit`, `EXIT_GRACE_S`, `DRAIN_S`, `STDERR_TAIL`; `televibe.lines.json_records`; `televibe.procinfo.kill_group`; `televibe.env.child_env`.
- Produces:
  - `televibe.oneshot.run_oneshot(argv: list[str], spec: AskSpec, env: dict[str, str], parser: Parser) -> Answer` — the process runner; raises `AskFailed`.
  - `_ClaudeParser.structured_output: Any` (None until a non-error `result` record with that key).
  - `ClaudeCode.ask(...) -> Answer`.

Note on the fake: `tests/fakes/fakeagent.py` replays `FAKE_FIXTURE` for any argv and records argv/cwd/env/prompt to `FAKE_RECORD`. It needs no change. The `{{SESSION_ID}}` placeholder becomes the literal `no-session-flag` (the fake's fallback when there is no `--session-id`/`--resume`), which the parser ignores for a one-shot.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ask_run.py`:

```python
"""One-shot calls against the fake claude (SPEC section 12, REQ-TEST-2)."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from conftest import FAKES_BIN, read_fixture, wait_dead, wait_file
from televibe import Account, AskFailed, ClaudeCode, Engine, FailReason, Limits, TelevibeError

SCHEMA = {"type": "object", "required": ["word"], "properties": {"word": {"type": "string"}}}
FIXTURES = Path(__file__).parent / "fixtures" / "claude"


@pytest.fixture
def claude(tmp_path, fake_path):
    return ClaudeCode(Account(tmp_path / "acc", credentials={"FAKE_CREDENTIAL": "s3cret"}))


def env(tmp_path, fixture="ask.jsonl", **knobs):
    out = {"FAKE_RECORD": str(tmp_path / "record.json")}
    if fixture is not None:
        out["FAKE_FIXTURE"] = str(FIXTURES / fixture)
    out.update({f"FAKE_{k.upper()}": str(v) for k, v in knobs.items()})
    return out


def record(tmp_path):
    return json.loads((tmp_path / "record.json").read_text())


async def test_ask_returns_the_structured_output(claude, tmp_path):
    """REQ-ASK-1, REQ-ASK-2, REQ-ASK-3: data is the result's structured_output; usage passed through;
    the fake saw the flags, the prompt on stdin, the account var and credential, and no API key."""
    answer = await claude.ask("Which word?", schema=SCHEMA, system="one word", model="sonnet", env=env(tmp_path))
    result = next(r for r in read_fixture("claude", "ask.jsonl") if r["type"] == "result")
    assert answer.data == result["structured_output"]
    assert answer.usage == result["usage"]
    rec = record(tmp_path)
    assert rec["prompt"] == "Which word?"
    assert rec["argv"][:5] == ["-p", "--output-format", "stream-json", "--verbose", "--json-schema"]
    assert "--tools" in rec["argv"] and rec["argv"][rec["argv"].index("--tools") + 1] == ""
    assert rec["argv"][-2:] == ["--system-prompt", "one word"]
    assert rec["env"]["CLAUDE_CONFIG_DIR"] == str(claude.account.home)
    assert rec["env"]["FAKE_CREDENTIAL"] == "s3cret"
    assert "ANTHROPIC_API_KEY" not in rec["env"]


async def test_ask_passes_the_schema_compact_and_utf8(claude, tmp_path):
    """REQ-ASK-2: the schema reaches argv as one compact JSON string with non-ASCII intact."""
    schema = {**SCHEMA, "description": "одно слово"}
    await claude.ask("hi", schema=schema, env=env(tmp_path))
    argv = record(tmp_path)["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema
    assert "одно слово" in argv[argv.index("--json-schema") + 1]


async def test_ask_runs_in_a_fresh_directory_that_is_removed(claude, tmp_path):
    """REQ-ASK-3: cwd is an empty temporary directory of the call's own, gone afterwards."""
    await claude.ask("hi", schema=SCHEMA, env=env(tmp_path))
    cwd = Path(record(tmp_path)["cwd"])
    assert cwd != Path.cwd() and cwd != claude.account.home and not cwd.is_relative_to(tmp_path)
    assert not cwd.exists()


async def test_ask_reports_limits(claude, tmp_path):
    """REQ-ASK-1: the last Limits of the call rides on the answer; ok.jsonl has one, ask.jsonl may not."""
    answer = await claude.ask("hi", schema=SCHEMA, env=env(tmp_path))
    records = read_fixture("claude", "ask.jsonl")
    if any(r["type"] == "rate_limit_event" for r in records):
        assert isinstance(answer.limits, Limits)
    else:
        assert answer.limits is None


async def test_ask_without_structured_output_is_provider_error(claude, tmp_path):
    """REQ-ASK-4: a result that has no structured_output (an ordinary turn's result) is provider_error."""
    with pytest.raises(AskFailed) as info:
        await claude.ask("hi", schema=SCHEMA, env=env(tmp_path, fixture="ok.jsonl"))
    assert info.value.reason is FailReason.PROVIDER_ERROR and "structured output" in info.value.detail


async def test_ask_refused_at_the_limit_is_usage_limit(claude, tmp_path):
    """REQ-ASK-4, REQ-CLAUDE-3: the recorded limit refusal is usage_limit."""
    with pytest.raises(AskFailed) as info:
        await claude.ask("hi", schema=SCHEMA, env=env(tmp_path, fixture="limit.jsonl", exit=1))
    assert info.value.reason is FailReason.USAGE_LIMIT


async def test_ask_exits_without_result_is_provider_error(claude, tmp_path):
    """REQ-ASK-4, REQ-RUN-5: no result record → provider_error with the stderr tail (a rejected schema looks like this)."""
    (tmp_path / "err.txt").write_text("Error: --json-schema is not valid JSON Schema\n")
    with pytest.raises(AskFailed) as info:
        await claude.ask("hi", schema=SCHEMA, env=env(tmp_path, fixture=None, exit=2, stderr=tmp_path / "err.txt"))
    assert info.value.reason is FailReason.PROVIDER_ERROR and "not valid JSON Schema" in info.value.detail


async def test_ask_spawn_error(tmp_path, monkeypatch):
    """REQ-ASK-4, REQ-RUN-4: a binary that cannot start is spawn_error, not an OSError."""
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    claude = ClaudeCode(Account(tmp_path / "acc"))
    with pytest.raises(AskFailed) as info:
        await claude.ask("hi", schema=SCHEMA, env=env(tmp_path))
    assert info.value.reason is FailReason.SPAWN_ERROR


async def test_ask_timeout_kills_the_group(claude, tmp_path):
    """REQ-ASK-3, REQ-ASK-4, REQ-CANCEL-4: past timeout_s the call is timeout and the child it started is dead."""
    pidfile = tmp_path / "child.pid"
    with pytest.raises(AskFailed) as info:
        await claude.ask("hi", schema=SCHEMA, timeout_s=0.5, env=env(tmp_path, mode="hang", child_pid=pidfile))
    assert info.value.reason is FailReason.TIMEOUT
    assert await wait_dead(int(pidfile.read_text()))
    assert await wait_dead(record(tmp_path)["pid"])


async def test_ask_cancel_kills_the_process(claude, tmp_path):
    """REQ-ASK-3, REQ-ASK-4: cancelling the awaiting task kills the process; CancelledError propagates, no AskFailed."""
    pidfile = tmp_path / "child.pid"
    task = asyncio.create_task(claude.ask("hi", schema=SCHEMA, env=env(tmp_path, mode="hang", child_pid=pidfile)))
    await wait_file(tmp_path / "record.json")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_dead(record(tmp_path)["pid"])
    assert await wait_dead(int(pidfile.read_text()))


async def test_ask_waits_for_exit_then_kills(claude, tmp_path):
    """REQ-ASK-3, REQ-RUN-7: a child that holds stdout after the result does not keep the call open."""
    pidfile = tmp_path / "child.pid"
    answer = await claude.ask("hi", schema=SCHEMA, env=env(tmp_path, mode="orphan", child_pid=pidfile))
    assert answer.data
    assert await wait_dead(int(pidfile.read_text()))


async def test_ask_takes_no_slot_and_leaves_no_marker(claude, tmp_path):
    """REQ-ASK-6: with the engine's only slot held by a hanging turn, ask() still returns, and the state dir holds one marker."""
    async with Engine(tmp_path / "state", max_concurrent=1) as engine:
        session = claude.new_session(tmp_path)
        turn_env = {"FAKE_FIXTURE": str(FIXTURES / "ok.jsonl"), "FAKE_MODE": "hang", "FAKE_RECORD": str(tmp_path / "turn.json")}
        async with engine.turn("hold the slot", session=session, env=turn_env) as turn:
            await wait_file(tmp_path / "turn.json")
            answer = await asyncio.wait_for(claude.ask("hi", schema=SCHEMA, env=env(tmp_path)), 10)
            assert answer.data
            markers = list((tmp_path / "state" / "turns").glob("*.json"))
            assert len(markers) == 1 and json.loads(markers[0].read_text())["turn_id"] == turn.id
            turn.cancel()
            async for _ in turn:
                pass
```

(`FAKE_STDERR` expects a file path; the test writes one. `wait_dead`, `wait_file`, `read_fixture`, `FAKES_BIN` and the `fake_path` fixture already exist in `tests/conftest.py` and `tests/replay.py`; `conftest` re-exports `read_fixture` via `from replay import FIXTURES, read_fixture`.)

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/test_ask_run.py -v`
Expected: every test FAILS with `TelevibeError: claude has no one-shot call in this version` (the base refusal from Task 3).

- [ ] **Step 3: Teach the Claude parser `structured_output`**

In `src/televibe/providers/claude.py`, `_ClaudeParser.__init__` add `self.structured_output: Any = None`. In `feed`, inside the `kind == "result"` branch's `else:` (the non-error result), add before `self.terminal = Done(...)`:

```python
                if "structured_output" in record:
                    self.structured_output = record["structured_output"]
```

- [ ] **Step 4: The runner**

Append to `src/televibe/oneshot.py`:

```python
import asyncio  # noqa: E402 - keep the module's data definitions above the runner
import tempfile  # noqa: E402

from televibe.lines import json_records  # noqa: E402
from televibe.procinfo import kill_group  # noqa: E402
from televibe.providers.base import Failure, Parser  # noqa: E402
from televibe.turn import DRAIN_S, EXIT_GRACE_S, STDERR_TAIL, _Tail, _feed_stdin, _leader_exit  # noqa: E402
```

Then move those imports to the top of the file instead (the `noqa` lines above are only to make the snippet self-contained; the committed file has one import block at the top, sorted, with `from televibe.providers.base import Failure, Parser` and `from televibe.turn import ...`). Check the cycle: `turn.py` imports `providers.base`, `providers.base` imports `oneshot` (for `Answer`), and `oneshot` would import `turn` and `providers.base` at module level — a cycle. Break it by importing inside the function:

```python
async def run_oneshot(argv: list[str], spec: AskSpec, env: dict[str, str], parser: Parser) -> Answer:
    """Run one call to its result (REQ-ASK-3, REQ-ASK-4). `env` is the full child environment."""
    from televibe.lines import json_records
    from televibe.procinfo import kill_group
    from televibe.providers.base import Failure
    from televibe.turn import DRAIN_S, EXIT_GRACE_S, STDERR_TAIL, _Tail, _feed_stdin, _leader_exit

    limits: list[Limits] = []

    async def read(stdout: asyncio.StreamReader) -> None:
        async for record in json_records(stdout):
            for event in parser.feed(record):
                if isinstance(event, Limits):
                    limits.append(event)
            if parser.terminal is not None:
                return

    with tempfile.TemporaryDirectory(prefix="televibe-ask-") as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise AskFailed(FailReason.SPAWN_ERROR, f"could not start {argv[0]!r}: {exc}") from None
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr = _Tail(STDERR_TAIL)
        stderr_task = asyncio.create_task(stderr.drain(proc.stderr))
        reader = asyncio.create_task(read(proc.stdout))
        exited = asyncio.create_task(_leader_exit(proc))
        timed_out = False
        try:
            async with asyncio.timeout(spec.timeout_s):
                await _feed_stdin(proc.stdin, spec.prompt)
                await asyncio.wait({reader, exited}, return_when=asyncio.FIRST_COMPLETED)
                if reader.done() and reader.exception() is None:
                    try:
                        await asyncio.wait_for(asyncio.shield(exited), EXIT_GRACE_S)
                    except TimeoutError:
                        kill_group(proc.pid)
                else:
                    await asyncio.wait({reader}, timeout=DRAIN_S)
        except TimeoutError:
            timed_out = True
        finally:
            kill_group(proc.pid)  # timeout, cancel, or stragglers holding the pipes (REQ-RUN-7)
            reader.cancel()
            await asyncio.wait({exited})
            await asyncio.wait({stderr_task}, timeout=DRAIN_S)
            stderr_task.cancel()
            await asyncio.gather(reader, stderr_task, return_exceptions=True)
        if not reader.cancelled() and (error := reader.exception()) is not None:
            raise error
        if timed_out:
            raise AskFailed(FailReason.TIMEOUT, f"no result within {spec.timeout_s:g} s")
        returncode = proc.returncode if proc.returncode is not None else -1
        outcome = parser.finish(returncode, stderr.text())
        if isinstance(outcome, Failure):
            raise AskFailed(outcome.reason, outcome.detail)
        data = getattr(parser, "structured_output", None)
        if data is None:
            raise AskFailed(FailReason.PROVIDER_ERROR, "the agent produced no structured output")
        return Answer(data, outcome.usage, limits[-1] if limits else None)
```

`asyncio` and `tempfile` go in the top import block (they create no cycle). Keep `Answer`, `AskFailed`, `AskSpec`, `check_ask_args` above `run_oneshot` in the file.

- [ ] **Step 5: `ClaudeCode.ask`**

In `src/televibe/providers/claude.py` add imports `from collections.abc import Mapping`, `from types import MappingProxyType`, `from televibe.env import child_env`, `from televibe.oneshot import Answer, AskSpec, check_ask_args, run_oneshot`, and in `class ClaudeCode`:

```python
    async def ask(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        system: str | None = None,
        model: str | None = None,
        env: Mapping[str, str] = MappingProxyType({}),
        timeout_s: float = 180,
    ) -> Answer:
        """One answer in `schema`: no tools, no session, no MCP, outside the engine (section 12)."""
        spec = check_ask_args(prompt, schema, system, model, env, timeout_s)
        child = child_env(self.account_var, self.account, spec.env)
        return await run_oneshot(self.ask_argv(spec), spec, child, self.parser(self.new_session(".")))
```

`self.parser(...)` needs a `Session`; the one-shot has none, so it gets a throw-away `new_session(".")` that is never started and never reported — the parser only uses it to build `Started`/`Done`, which `run_oneshot` ignores. If this reads wrong, add a module-level comment saying so rather than widening `Parser`'s constructor.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_ask_run.py tests/test_ask.py -v`
Expected: PASS, all 12 + 7.

Run the whole suite: `uv run pytest -q`
Expected: everything green except `tests/test_requirements.py` — REQ-ASK-1..6 are now all claimed, so it is green too. If it names a missing id, add the id to the docstring of the test that exercises it.

Run: `uvx ruff check src tests && uvx ty check`
Expected: clean in `src/`.

- [ ] **Step 7: Revert-and-watch (the discrimination ritual)**

Comment out the `if "structured_output" in record:` lines from Step 3, run `uv run pytest tests/test_ask_run.py::test_ask_returns_the_structured_output -q` and watch it fail with `AskFailed: provider_error: the agent produced no structured output`. Restore. Then replace `kill_group(proc.pid)` in the `finally` with `pass`, run `test_ask_timeout_kills_the_group`, watch it fail on `wait_dead`. Restore.

- [ ] **Step 8: Commit**

```bash
git add src/televibe/oneshot.py src/televibe/providers/claude.py tests/test_ask_run.py
git commit -m "oneshot: ClaudeCode.ask() runs a tool-less, sessionless call outside the engine"
```

---

### Task 5: Live test, release 0.4.0

**Files:**
- Modify: `tests/test_live.py` (one test)
- Modify: `pyproject.toml` (version), `uv.lock` (`uv lock`)
- Modify: `README.md` ("Status: 0.1, alpha" line → 0.4)

**Interfaces:**
- Consumes: `ClaudeCode.ask` (Task 4).

- [ ] **Step 1: Add the live test**

Append to `tests/test_live.py`:

```python
async def test_live_one_shot_call(tmp_path):
    """REQ-ASK-1, REQ-ASK-2: a real tool-less call answers in the schema."""
    home = os.environ.get("TELEVIBE_LIVE_CLAUDE_HOME")
    if not home:
        pytest.skip("set TELEVIBE_LIVE_CLAUDE_HOME to run")
    token = os.environ.get("TELEVIBE_LIVE_CLAUDE_TOKEN")
    claude = ClaudeCode(Account(home, credentials={"CLAUDE_CODE_OAUTH_TOKEN": token} if token else None))
    answer = await claude.ask(
        "Which word do people say to agree? Answer with that word only.",
        schema={"type": "object", "additionalProperties": False, "required": ["word"],
                "properties": {"word": {"type": "string"}}},
        system="Answer with one English word in the field word.",
        model="sonnet",
    )
    assert isinstance(answer.data, dict) and answer.data["word"].strip().lower() in {"yes", "ok", "okay", "agreed", "yeah"}
    assert answer.usage
```

Run: `TELEVIBE_LIVE_CLAUDE_HOME=~/.claude-televibe uv run pytest -m live tests/test_live.py::test_live_one_shot_call -v`
Expected: PASS against the real CLI (it costs one subscription call). Without the variable it SKIPs.

- [ ] **Step 2: Bump the version**

In `pyproject.toml` set `version = "0.4.0"`, run `uv lock`. In `README.md` change `Status: 0.1, alpha.` to `Status: 0.4, alpha.`.

- [ ] **Step 3: Full suite and lint**

Run: `uv run pytest -q && uvx ruff check src tests && uvx ty check`
Expected: all green (two live tests skipped without the env, three now including the new one).

- [ ] **Step 4: Release commit, merge to main, push**

```bash
git add tests/test_live.py pyproject.toml uv.lock README.md
git commit -m "Release 0.4.0: one-shot calls — provider.ask() answers in a JSON schema with no session and no tools

A bot around televibe has model-only chores — cleaning a speech transcript,
classifying a message — that a turn is the wrong tool for: a turn opens a
session, may use tools, and waits in the engine's queue behind real turns.
Until now a bot hand-rolled the call.

ClaudeCode.ask(prompt, schema=..., system=..., model=..., env=..., timeout_s=...)
runs claude -p with --json-schema, --tools \"\", --strict-mcp-config and
--no-session-persistence in a throw-away directory, with a turn's environment
rules and process-group kill, and returns Answer(data, usage, limits) or raises
AskFailed(reason, detail) with a turn's failure reasons. It takes no slot, lane
or session lock and writes no marker. Codex.ask refuses until a recording exists."
```

Then from the **main checkout**:

```bash
cd ~/my/televibe && git status --short   # must be clean, on main
git merge --ff-only work/ask && git push origin main
```

Wait for `ci.yml` (ubuntu + macos × 3.12/3.13) to go green: `gh run watch` or `gh run list --limit 3`.

- [ ] **Step 5: Tag — gated**

Pushing the tag publishes to PyPI (`publish.yml`, trusted publishing). Do it only on Maxim's explicit yes in chat:

```bash
cd ~/my/televibe && git tag v0.4.0 && git push origin v0.4.0
```

Then confirm `https://pypi.org/project/televibe/0.4.0/` exists. Remove the workspace with `bash ~/my/us/plugins/us/lib/wt.sh remove-if-safe <path>` once merged.

---

## Self-review notes

- **Spec coverage:** REQ-ASK-1 (test_ask_returns_the_structured_output, test_codex_has_no_one_shot_call, live), REQ-ASK-2 (test_ask_argv, test_ask_passes_the_schema_compact_and_utf8), REQ-ASK-3 (fresh directory, cancel, timeout, orphan tests), REQ-ASK-4 (provider_error ×2, usage_limit, spawn_error, timeout, cancel), REQ-ASK-5 (misuse parametrized, surrogate), REQ-ASK-6 (test_ask_takes_no_slot_and_leaves_no_marker). REQ-API-1 (surface test updated).
- **Type consistency:** `AskSpec` fields are `prompt, schema_json, system, model, env, timeout_s` everywhere; `run_oneshot(argv, spec, env, parser)` in Task 4 matches the call in `ClaudeCode.ask`; `Answer(data, usage, limits)` positional order is the same in `oneshot.py`, the tests and the README.
- **Open risk, named:** Task 2's measurement decides between `stream-json` and `json`. The plan carries the fallback inline rather than pretending the result is known.
