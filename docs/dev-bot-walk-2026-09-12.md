# The dev-bot walk — the Claude meta layer

Everything in `telegrind/meta/` has only ever run against fakes. No real
`claude` subprocess, no real Telegram, no real Postgres. This is the walk that
retires that, ordered by where the branch is most likely to be wrong.

Written 2026-09-12 by the branch's final review, against `metheoryt/claude-meta-1-4`.
Every end-to-end walk in this repo's history has found a defect the unit suite
structurally could not see. Do not skip it because the suite is green.

Phase 0 and Phase 1 cost nothing and risk nothing. Do all of both before Phase 2.

---

## Phase 0 — before the bot starts

**0.1 Point `CLAUDE_CWD` at a scratch checkout.**

```sh
git worktree add /tmp/claude-sandbox HEAD
# then in .env:  CLAUDE_CWD=/tmp/claude-sandbox
```

Unset, `cwd` falls back to the bot's own working directory — so the first
`talk` message runs full Claude Code, with your CLAUDE.md, skills, hooks and
MCP servers, under `--permission-mode bypassPermissions --permission-prompts
none`, **in the tree you are working in**, uncommitted work and all.
*Failure looks like:* your branch acquiring commits, edits or a deleted file
you did not make. *Means:* you ran a bypassPermissions agent on your own work.

**0.2 Migrate and check the backfill.**

```sh
uv run alembic upgrade head
# then: SELECT verdict, count(*) FROM message GROUP BY 1;
```

*Failure:* a column error, or every pre-existing row `fact` when some were `/q`.
*Means:* the backfill `CASE` did not match reality.

**0.3 Confirm "off" is a supported state.** Start with `CLAUDE_ADMIN_CHAT_IDS`
unset, send one message, read `SELECT chat_id FROM chat;` for your id, then set
it. Nothing should crash without a sessionmaker.

---

## Phase 1 — a fake `CLAUDE_BIN` (zero tokens, zero risk)

```sh
cat > /tmp/fakeclaude <<'EOF'
#!/bin/sh
printf '%s\n' "$@" >> /tmp/claude-argv.log
case "$FAKE_MODE" in
  slow)    sleep 300 ;;
  exit1)   echo "No conversation found with session ID: x" >&2; exit 1 ;;
  kill)    kill -9 $$ ;;
  iserror) echo '{"is_error":true,"result":"boom","session_id":"s"}' ;;
  garbage) echo 'not json' ;;
  empty)   echo '{"is_error":false,"result":"","session_id":"s"}' ;;
  long)    python3 -c 'import json;print(json.dumps({"is_error":False,"result":"x"*5000}))' ;;
  mixed)   case "$*" in
             *ЖДИ*) sleep 300 ;;
             *) sleep 5; echo '{"is_error":false,"result":"ок","session_id":"s"}' ;;
           esac ;;
  html)    echo '{"is_error":false,"result":"if a < b && c > d then <b>ok</b>"}' ;;
  *)       echo '{"is_error":false,"result":"ок","session_id":"s"}' ;;
esac
EOF
chmod +x /tmp/fakeclaude   # then CLAUDE_BIN=/tmp/fakeclaude
```

**1. The reply chain, against real Telegram.** *Highest risk — never exercised.*
Send a `talk` message, get the reply, reply to that reply. `/tmp/claude-argv.log`
must show `--resume <uuid> --fork-session` on the second turn.
`meta_wiring.parent_of` reads `reply_to_message.message_id` out of the `raw`
JSONB of the row `outbound.say` stored from Telegram's *response* object — no
test has ever seen a real one. *Failure:* no `--resume` on turn two. *Means:*
`sent.model_dump()` does not carry `reply_to_message`, every turn starts cold,
and the session design is inert.

**2. The question path, end to end.** Three facts, then «сколько я потратил
сегодня» with no `/q`. Expect three 💔, a `Разбираю 3 сообщений…` notice, then
the answer as a reply. The notice is the only `say` with `reply_to=None` — the
false arm of `ReplyParameters(...) if reply_to else None`, never exercised.
*Failure:* a Telegram 400 on the notice.
Then `SELECT message_id, verdict, receipt_emoji FROM message ORDER BY id DESC
LIMIT 6;` — the bot's own two rows `system` with no receipt, the question
`question` with `receipt_emoji IS NULL`.

**3. `--` and argv.** Send a `talk` message that is literally `--version`.
*Failure:* the reply is the CLI's version string. *Means:* the `--` separator
was lost, i.e. argv injection into a bypassPermissions subprocess.

**4. Queue legibility.** `FAKE_MODE=slow`, `CLAUDE_TURN_TIMEOUT=20`. Send a
`talk` message, immediately reply to it. Expect: the first gets 👀, the second
stays **bare**; after 20s a timeout message; then the second runs. *Failure:*
both get 👀 at once, or the second never runs. *Means:* per-key serialisation
or the in-flight cleanup is wrong against a real loop.

**5. Two threads at once.** Two unrelated `talk` messages in quick succession →
both 👀, two turns, two `--session-id`s in the log. *Failure:* serialised.
*Means:* the slot key is wrong.

**6. Every failure variant, one at a time.** `exit1` on a *fresh* turn (must NOT
retry), `exit1` on a *resumed* turn (must retry cold — two log entries, the
second with no `--resume`), `kill` (negative code → no retry), `garbage`,
`iserror`, `empty`, `slow` past the timeout. Each must (a) take the 👀 off and
(b) say something in Russian. *Failure:* a 👀 left lit with no message.

**7. All six edit transitions.** fact→fact (💔→❤‍🔥→💘), fact→talk (heart
clears, then 👀), talk→fact (👀 clears, heart appears), fact→question, an edit
of a `/q`, and an edit **after** 👀 — which must change nothing on screen, while
`SELECT text, verdict, receipt_emoji` shows the new text, the old verdict, and
👀 still set. *Failure of the last:* a second Claude answer. *Means:* the point
of no return leaks.

**8. The tombstone-on-edit arm.** Send a fact, ask a question (forces
extraction), confirm the fact counts, then edit that message into a question
and re-ask. *Failure:* the old number still counts. *Means:* the
`elif previous is not None` arm did not fire against real SQL.

**9. Reactions still arrive.** Tap 💔 on an extracted fact, re-ask, untap,
re-ask. *Failure:* nothing happens. *Means:* `allowed_updates` lost
`message_reaction` — the failure `setup.py`'s docstring warns about.

**10. Markup and length.** `FAKE_MODE=html` → the reply arrives verbatim with
the angle brackets. `FAKE_MODE=long` → arrives truncated at 4096, not dropped.
*Failure:* nothing arrives. *Means:* `parse_mode=None` is not reaching the API.

**11. A captioned photo.** Send a photo captioned «сколько я потратил на это».
`message.text` is None on every path here — `record`, `route` and
`record_edited` all rely on `text or caption`. *Failure:* «Спроси что-нибудь
после /q.» in reply to a photo. *Means:* one of the three reads only `.text`.

**12. A bad `ANTHROPIC_API_KEY`, deliberately.** Point it at a bad key and send
a plain question. Expect a sentence in the chat saying it failed — silence with
a bare bubble means the reply path still swallows the update.

**13a. Ctrl-C mid-turn, deliberately — and the startup sweep that recovers it.**
This is the case the sweep is *for*: one killed turn, and nothing in the chat
after it. Read 13b before concluding anything from it — on its own it passes
while the sweep is much narrower than it looks.
`FAKE_MODE=slow`, send a `talk` message, kill the bot mid-turn. `SELECT
receipt_emoji FROM message WHERE message_id = <it>;` → `👀`: the marker outlives
the process, and nothing in a dead worker will ever take it off.

Restart. Before polling starts the log must say `released 1 stranded
hand-over(s): [<it>]`, and the same `SELECT` must now read NULL. **The bubble
still shows 👀 — that is expected**, the sweep touches the row only. Now edit
that message: it must be re-classified and handed over again, a new turn must
answer it, and 👀 must be re-placed by the new turn.
*Failure:* the edit is ignored and the log says `edit after hand-over`. *Means:*
either the sweep did not run (it is called from `main.py`, and it swallows every
exception — check the log for `could not release stranded hand-overs`), or a
protective row shielded the message: any row the bot itself stored later in that
chat — a `/q` answer, a `Разбираю N сообщений…` notice, or the answer to another
turn (13b). The second is the predicate erring on its safe side, not a bug — but
confirm which one it was before touching anything.

The 👀 also has to leave the bubble, not just the column. The sweep cannot
touch reactions, so after the restart the screen and the row disagree until
something re-places or clears the receipt — and the edit above is that
something. Edit the released message into a **question** («сколько я потратил
сегодня») rather than talk: expect the answer as a reply *and* the 👀 gone.
*Failure:* the answer arrives under a 👀 nothing is behind. *Means:* the
unconditional clear in `record_edited` is not firing, and the receipt is
claiming a turn that does not exist.

Then the other direction, which is the one that must not be wrong. Let a `talk`
message be answered normally, restart, and check that message's
`receipt_emoji` is **still 👀** and that editing it changes nothing on screen.
*Failure:* a second answer to a message Claude already answered. *Means:* the
sweep released an answered row and re-opened the double hand-over `claim`
exists to prevent — and most likely because the bot's own stored row does not
carry `raw['from_user']['is_bot']` after all. **That is the one fact the sweep
rests on that no test in the suite can reach**, and this is the only item that
retires it. `SELECT raw->'from_user'->>'is_bot' FROM message WHERE verdict =
'system' ORDER BY id DESC LIMIT 5;` answers it directly.

**13b. Two turns at once, only the second killed — where the sweep stops.**
*This is the boundary, and 13a cannot see it.* `FAKE_MODE=mixed`: that branch
sleeps only when the prompt contains `ЖДИ`, so one turn can finish while
another hangs.

Send M1 «привет как дела» (answers after 5s), then immediately M2 «ЖДИ» as an
**unrelated thread** — not a reply, so the slot key differs and the two run in
parallel (item 5). Both get 👀. At ~5s M1 is answered, and `outbound.say` stores
the bot's own answer as a row of its own. Kill the bot while M2 is still
hanging.

```sql
SELECT id, message_id, verdict, receipt_emoji FROM message ORDER BY id;
```
Expect M1, then M2, then the answer to M1 — `verdict = 'system'`, a **larger
`id` than M2**, and both M1 and M2 still carrying 👀.

Restart. The log must **not** say `released ... stranded hand-over(s)` (nothing
is logged when nothing is released), M2's `receipt_emoji` must still read 👀,
and editing M2 must log `edit after hand-over`. **That is the documented
behaviour, not a failure:** M2 is shielded by an answer belonging to a
different turn, so it stays stranded for the life of the database and only a
new message recovers it — the sweep recovers the stranded messages *after the
chat's last bot row*, which here is M1's answer.
*Failure:* M2 **is** released. *Means:* the predicate is not the one
`release_hand_overs` documents — most likely the bot's own row is not being
recognised as the bot's (`SELECT raw->'from_user'->>'is_bot'`), which is the
same fact 13a's second half rests on and the direction that must not be wrong.

---

## Phase 2 — the real binary (costs tokens; `CLAUDE_CWD` still the sandbox)

**14. One `talk` turn.** A `.jsonl` should appear under
`~/.claude/projects/<slug-of-CLAUDE_CWD>/` named with the derived uuid5, and
`META_SYSTEM` should have shortened the answer. *Failure:* a three-paragraph
essay. *Means:* `--append-system-prompt` is not landing.

**15. The three-turn chain — the single most valuable item, and nothing tests
it.** M1 → A1 → reply F1 → A2 → reply **to A2** F2 → A3. Ask in F2 something
only turn 1 could answer. *Failure:* A3 does not know. *Means:*
`--resume <base> --fork-session --session-id <new>` does not carry memory the
way the one 2026-09-12 measurement said it does, and the session design needs
revisiting.

**16. Retry collision.** Delete a base transcript `.jsonl`, then reply into that
thread. The first spawn should fail on the dead resume and the retry should run
with the same `--session-id`. *Failure:* the retry also fails, with "session
already exists". *Means:* the cold retry is only safe when the first attempt
created no session file — a narrow assumption nothing has verified.

**17. Reply to a `/q` answer** with «а почему». Expect one wasted `exit 1`
spawn, then a cold turn that answers. The bot's own `/q` answers are stored the
way Claude's are, so the two-hop resolves to a session that never existed.
*Failure:* a crash string in the chat.

**18. Force a real timeout.** `CLAUDE_TURN_TIMEOUT=5`, then `ps -ef | grep -c
claude`. *Failure:* orphaned node / MCP-server processes surviving the kill.
*Means:* the spawn needs its own process group.

**19. A refused question.** Ask something outside the closed aggregate set
(«что ты думаешь о моих тратах»). Expect no number and a Claude answer.
*Failure:* «Не понял вопрос, переформулируй.» while the layer is on. *Means:*
the fall-through to the hand-off is not wired.

---

## What this walk is retiring

Two facts the whole meta layer rests on are **one measurement each, from
2026-09-12, with no test and no second observation**: that
`--resume <base> --fork-session --session-id <new>` carries memory, and that a
dead resume exits 1 before any model call. If either is wrong, nothing in the
suite notices and the symptom is a bot that answers each turn as if the
conversation had not happened. Item 15 is what retires the first; nothing else
can.

The second-largest gap is the join nobody has seen: the bot's own outbound row
is the only source of the reply chain's second hop, and it is built from a
Telegram *response* object no fake has ever imitated. Item 1 retires it in two
messages.
