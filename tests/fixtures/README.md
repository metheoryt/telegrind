# Fixtures

`claude/` holds recorded Claude Code stream-json output, copied from televibe (`~/my/televibe/tests/fixtures/claude/`). The fake `claude` in `tests/fakes/` replays them.

| File | Copied from | CLI version | What it is |
|---|---|---|---|
| `claude/turn.jsonl` | `ok.jsonl` | Claude Code 2.1.294 | New session (`--session-id`), plan mode, one `Read` tool use, success. |
| `claude/limit.jsonl` | `limit.jsonl` | Claude Code 2.1.294 | New session refused at the five-hour usage limit (`usage_limit_reached`), exit code 1. |
| `claude/ask.jsonl` | recorded for telegrind | Claude Code 2.1.301 | The voice cleanup call (`cleanup.SCHEMA`, `cleanup.SYSTEM` with no keyterms) on «четыре пятьсот такси вчера ну вечером»; `structured_output: {"text": "Вчера вечером потратил 4500 на такси.", "question": false}`. |
| `claude/ask_question.jsonl` | recorded for telegrind | Claude Code 2.1.301 | The same call on «сколько я потратил на такси в сентябре»; `structured_output: {"text": "Сколько я потратил на такси в сентябре?", "question": true}`. |

The two `ask*` files were recorded on 2026-10-16 with a real call on the subscription. The argv is televibe's own
(`check_ask_args` + `ClaudeCode.ask_argv`, prompt `cleanup.prompt_for(raw)`, system `cleanup.system_for([])`, model
`sonnet`) plus `--setting-sources project`, run from an empty directory outside `$HOME` with every `CLAUDE_CODE_*`
variable unset. The extra flag keeps the recording box's user-level SessionStart hooks out of the call: without it
the stream opened with 18 `hook_started`/`hook_response` records carrying personal memory, which production never
sees. They were sanitized with televibe's `tests/fixtures/sanitize.py`, and the init record's
`messaging_socket_path` (a `/run/user/...` socket the sanitizer misses) was set to `{{PATH}}` by hand.
