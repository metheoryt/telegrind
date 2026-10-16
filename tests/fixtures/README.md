# Fixtures

`claude/` holds recorded Claude Code stream-json output, copied from televibe (`~/my/televibe/tests/fixtures/claude/`). The fake `claude` in `tests/fakes/` replays them.

| File | Copied from | CLI version | What it is |
|---|---|---|---|
| `claude/turn.jsonl` | `ok.jsonl` | Claude Code 2.1.294 | New session (`--session-id`), plan mode, one `Read` tool use, success. |
| `claude/limit.jsonl` | `limit.jsonl` | Claude Code 2.1.294 | New session refused at the five-hour usage limit (`usage_limit_reached`), exit code 1. |
| `claude/ask.jsonl` | `ask.jsonl` | Claude Code 2.1.301 | One-shot call with `--json-schema`; the `result` carries `structured_output: {"word": "Yes"}`. |

`ask.jsonl` is a generic answer, not telegrind's cleanup schema. A test that needs a specific shape must point `FAKE_ASK_FIXTURE` at its own file.
