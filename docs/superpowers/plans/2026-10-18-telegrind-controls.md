# telegrind — Buttons, forms and polls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aicha asks one question per answer, and asks it with Telegram controls: [Да] under an offer, a form of tick buttons, or a poll. A press, a [Готово] or a vote is logged and answered as the person's reply.

**Architecture:** televibe 0.7 (the companion plan, `2026-10-18-televibe-controls.md`, done first) parses the agent's control block and sends it. telegrind turns `Presenter(controls=CONTROL_TEXTS)` on, splits the block off before the editor, records each control and every press or vote as events on the bot's entry, and runs a press, a vote or a text reply to a control as a question turn continuing the answered message's chain. A newer answer in a chain closes the controls left open in it.

**Tech Stack:** Python 3.12+, aiogram 3.31, televibe `[telegram]` 0.7, SQLite. Tooling: `uv run pytest`, `uvx ruff check telegrind tests`, `uvx ruff format --check telegrind tests`, `uvx ty check`; live gate `TELEGRIND_LIVE_HOME=~/.claude-telegrind uv run pytest -m live`.

**Spec:** `docs/superpowers/specs/2026-10-18-buttons-and-polls-design.md` and `SPEC.md`, which each task amends with its own requirements before the code (`SPEC.md` is the law).

**Where the work happens:** the `us:worktrees` workspace already made for the design, `~/orca/workspaces/telegrind/buttons-and-polls` (branch `work/buttons-and-polls`). Never the main checkout.

## Global Constraints

- **Never a bare `uv sync`:** always `uv sync -p /usr/bin/python3.12` (or the box's non-freethreaded python3.14).
- **Dependency during development:** televibe from the local televibe workspace through a `[tool.uv.sources]` path entry (`televibe = { path = "/home/me/wt/televibe/controls", editable = true }` — the path the televibe plan's `wt.sh create` printed). That entry exists only on this branch and is removed in Task 6; a path source on `main` breaks the latitude image build.
- REQ-LOG-1: every update the bot acts on is committed before anything else — before `answer_callback_query`, an edit of a keyboard, `stop_poll`, or a queued turn.
- `log.sqlite` is used from the event-loop thread only. A new table is `CREATE TABLE IF NOT EXISTS` in `SCHEMA`; no new column on `entries`.
- REQ-TEST-1: every new `REQ-*` in `SPEC.md` appears in a test name or docstring.
- Every fixed sentence or label the bot shows is Russian, Aicha's feminine where she speaks of herself, gender-neutral about the person, and ends without a period (REQ-TURN-6).
- `BOT_TOKEN` and `ELEVENLABS_API_KEY` never reach a turn's env (unchanged).
- Commits carry no `Co-Authored-By` trailer.

## Review Focus

1. **A press delivered twice** (Telegram retries an unanswered callback, or a double tap) — one turn, not two: the second `record_press` finds the control already answered.
2. **A press on a control of a message the log never saw, or by someone not allowed** — the spinner stops (`answer_callback_query`), nothing is stored, no turn.
3. **A poll vote retracted** (`option_ids == []`) — logged as `voted`, starts nothing, the poll stays open.
4. **A voice reply to a form** — the transcript is the typed text and the ticks are merged in, exactly as a text reply does.
5. **A tick, then a restart, then [Готово]** — the ticks come back from the log's `pressed` events, not from memory.

Each line has its test in the task that owns the code (Tasks 1, 3).

---

### Task 1: Controls in the log

**Files:**
- Modify: `pyproject.toml` (path source, see Global Constraints; then `uv sync -p /usr/bin/python3.12`), `telegrind/log.py`, `SPEC.md` §3 (event kinds, the `polls` table, REQ-LOG-9)
- Test: `tests/test_log_controls.py`

**Interfaces:**
- Produces, in `telegrind/log.py`:
  ```python
  SCHEMA += "CREATE TABLE IF NOT EXISTS polls(poll_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL);"
  @dataclass(frozen=True, slots=True)
  class ControlRecord:
      message_id: int
      block: str                 # televibe control_block(...) text
      poll_id: str | None
      open: bool                 # a `control` event and no `answered` or `closed` after it
      presses: tuple[str, ...]   # callback data of every `pressed` event, in order
  class Log:
      def record_control(self, message_id: int, block: str, poll_id: str | None, at: int) -> None
      def control(self, message_id: int) -> ControlRecord | None
      def record_press(self, message_id: int, data: str, payload: dict[str, Any], at: int, *, closes: bool) -> bool
      def record_vote(self, poll_id: str, payload: dict[str, Any], at: int, *, closes: bool) -> tuple[int, bool] | None
      def close_control(self, message_id: int, how: str, at: int) -> bool
      def open_controls(self, chain_id: str) -> list[ControlRecord]
  ```
- Event kinds added to the `events` comment in `log.py` and SPEC §3: `control` `{"block", "poll_id"}`, `pressed` `{"data", "update"}`, `voted` `{"update"}`, `answered` `{}`, `closed` `{"how": "text" | "superseded"}`.
- `record_press` appends `pressed` (if the entry has a control), and in the same transaction `answered` when `closes` is true and the control is open; it returns whether it closed it. `record_vote` resolves `poll_id` through `polls`, appends `voted`, and `answered` the same way; `None` for an unknown poll. `close_control` appends `closed` only to an open control and returns whether it did.

- [ ] **Step 1: Write the failing tests** (real SQLite file in `tmp_path`, as `tests/test_log.py` does; a bot entry made with `record_bot_sent`)

```python
def test_control_is_open_until_answered(store):
    """REQ-LOG-9: a recorded control is open; an [offer] press closes it once; a second press closes nothing."""
    store.record_bot_sent(500, "Слить?", 10, {}, 1)
    store.record_control(500, "```control\noffer Да\n```", None, 2)
    assert store.control(500).open
    assert store.record_press(500, "tv:o", {"id": "q1"}, 3, closes=True) is True
    assert store.record_press(500, "tv:o", {"id": "q2"}, 4, closes=True) is False   # Review Focus 1
    rec = store.control(500)
    assert not rec.open and rec.presses == ("tv:o", "tv:o")
    assert [e.kind for e in store.events(500)] == ["bot_sent", "control", "pressed", "answered", "pressed"]

def test_ticks_are_kept_as_presses(store):
    """REQ-LOG-9: a tick keeps the control open; its data stays in order for replay (Review Focus 5)."""
    ...  # record a form control, two tick presses with closes=False → open, presses == ("tv:t:0:1", "tv:t:1:0")

def test_vote_resolves_the_poll_and_retraction_starts_nothing(store):
    """REQ-LOG-9: polls maps poll_id to the poll message; an empty vote is logged and closes nothing."""
    store.record_bot_sent(501, "Что из этого займы?", 500, {}, 1)
    store.record_control(501, POLL_BLOCK, "p-1", 2)
    assert store.record_vote("p-1", {"option_ids": []}, 3, closes=False) == (501, False)   # Review Focus 3
    assert store.record_vote("p-1", {"option_ids": [0]}, 4, closes=True) == (501, True)
    assert store.record_vote("nope", {}, 5, closes=True) is None

def test_close_and_open_controls_of_a_chain(store):
    """REQ-LOG-9: open_controls lists a chain's open ones; close_control closes once."""
    ...  # two controls on messages linked to chain "c", one closed → open_controls("c") is the other;
         # close_control(...) True then False; the event payload holds "how"

def test_press_on_a_message_without_a_control_stores_nothing(store):
    store.record_bot_sent(502, "ok", 10, {}, 1)
    assert store.record_press(502, "tv:o", {}, 2, closes=True) is False
    assert [e.kind for e in store.events(502)] == ["bot_sent"]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_log_controls.py -v` — FAIL.
- [ ] **Step 3: Implement** the table, the dataclass and the methods in `log.py`, each write in one `_tx()`.
- [ ] **Step 4: Amend `SPEC.md`** §3: the five event kinds in the `events` comment, `polls(poll_id, message_id)` in the bookkeeping paragraph, REQ-LOG-9 (a control and every press or vote on it are events on the bot's entry; open until `answered` or `closed`; a press and its `answered` in one transaction; the `polls` table resolves a vote).
- [ ] **Step 5: Run** `uv run pytest -q && uvx ruff check telegrind tests && uvx ty check` — pass.
- [ ] **Step 6: Commit** `git commit -m "Log: controls, presses and votes as events on the bot's entry (REQ-LOG-9)"`.

### Task 2: Answers carry controls

**Files:**
- Modify: `telegrind/agent.py`, `telegrind/app.py` (`say` strips a block), `tests/tgkit.py` (`FakeBot.send_poll` returning a message with `poll.id == f"poll-{message_id}"`, `stop_poll`, `edit_message_reply_markup`, `answer_callback_query`), `SPEC.md` §7 (REQ-TURN-13, REQ-TURN-14; REQ-TURN-12 amended)
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `televibe.telegram.controls` (`split_control`, `control_block`, `Offer`, `Poll`, `Form`, `ControlTexts`, `poll_fallback_text`); Task 1's `record_control`, `open_controls`, `close_control`.
- Produces, in `telegrind/agent.py`:
  ```python
  CONTROL_TEXTS = ControlTexts(
      offer_label="Да", done_label="Готово",
      pressed="Ответ кнопкой: {label}", voted="Выбрано в опросе: {options}", nothing="ничего",
      form="Анкета: {rows}", unset="не выбрано", typed="Текстом: {text}",
      answered="✓ {label}", answered_in_text="✓ ответ текстом", form_prompt="↑",
  )
  ```
  and the question turn's `Presenter(..., controls=CONTROL_TEXTS)`.

Behaviour to pin:
- `_Watched`: on `Done`, split the block off **before** the editor; the editor sees and rewrites the text alone; the event's text becomes `edited + "\n\n" + control_block(control)`; `draft` keeps the agent's full text, block included. Without an editor the event passes through. `shown()` returns the visible text — the block stripped — for `Done` and `Failed` alike, so `entries.text` never holds raw markup.
- After `show()`: for an `Offer` or `Form`, `record_control(sent.message_id, control_block(c), None, at)`. For a `Poll`, the poll message is stored like any bot message (`record_bot_sent(poll.message_id, poll_fallback_text(poll), sent.message_id, raw, at)`), linked to the chain (`chains.link`), and `record_control(poll.message_id, control_block(poll), poll_msg.poll.id, at)`.
- **Supersede:** after any sent answer, every `open_controls(chain.id)` other than the new one is closed: `stop_poll` for a poll, `edit_message_reply_markup(reply_markup=None)` otherwise (a Telegram failure is logged and ignored), then `close_control(id, "superseded", at)`.
- `App.say` sends `split_control(text)[0]`: a pass digest never shows a block (REQ-TURN-14).

- [ ] **Step 1: Write the failing tests** (the `rig` fixture and fake `claude` of `tests/test_agent.py`; a scripted answer ending in a block)

```python
async def test_offer_block_becomes_a_button_and_a_control_event(rig):
    """REQ-TURN-13: the answer goes out without the block, with [Да]; the entry text is visible text; a control event."""
    ...  # answer text "Слить 31 пару?\n\n```control\noffer\n```"
    [(_, kw)] = rig.bot.sends()
    assert kw["reply_markup"].inline_keyboard[0][0].text == "Да"
    entry = rig.store.entry(SENT_ID)
    assert entry.text == "Слить 31 пару?" and rig.store.control(SENT_ID).open

async def test_editor_never_sees_the_block(rig):
    """REQ-TURN-12, REQ-TURN-13: the editor's input has no control fence; the drafted event keeps it."""
    ...  # capture the editor's draft argument; assert "```control" not in it; drafted payload has it

async def test_poll_message_is_stored_linked_and_mapped(rig):
    """REQ-TURN-13: the poll message is a bot entry replying to the answer, in the chain, and in polls."""
    ...  # assert store.entry(poll_id_msg).reply_to == SENT_ID; chain_of(poll msg) == chain of the answer;
         # store.record_vote(f"poll-{poll_msg_id}", {...}, ...) resolves to it

async def test_newer_answer_closes_open_controls_of_its_chain(rig):
    """REQ-TURN-13: a reply in the chain answered → the older [Да] is removed and closed as superseded."""
    ...  # first answer with an offer; reply to it in text; second answer → edit_message_reply_markup(reply_markup=None)
         # on the first; control(first).open is False; last event payload {"how": "superseded"}

async def test_pass_digest_never_shows_a_block(rig):
    """REQ-TURN-14: a digest with a block is posted without it and records no control."""
```

- [ ] **Step 2: Run** them — FAIL.
- [ ] **Step 3: Implement** the behaviour above in `agent.py` and `App.say`.
- [ ] **Step 4: Amend `SPEC.md`**: REQ-TURN-12 (the editor gets the text without the control block; the block is re-attached after it; the stored text is the visible one), REQ-TURN-13 (controls on for question turns; what is recorded for an offer, a form, a poll; supersede), REQ-TURN-14 (pass digests carry no control; a block is stripped).
- [ ] **Step 5: Run** the full suite, ruff, ty — pass.
- [ ] **Step 6: Commit** `git commit -m "Answers carry an offer, a form or a poll; a newer answer closes the old ones (REQ-TURN-13, 14)"`.

### Task 3: Presses, votes and replies to a control are answers

**Files:**
- Modify: `telegrind/app.py`, `telegrind/agent.py` (`Agent.answer(..., answering: int | None = None)`; `Answerer` protocol in `app.py` likewise), `telegrind/bot.py`, `telegrind/cli.py` (`allowed_updates=["message", "edited_message", "message_reaction", "callback_query", "poll_answer"]`), `SPEC.md` §6 (REQ-TRIGGER-6, REQ-TRIGGER-7), `tests/tgkit.py` (`callback(...)` and `poll_vote(...)` update builders)
- Test: `tests/test_app_controls.py`, `tests/test_agent.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1's log methods, Task 2's `CONTROL_TEXTS`; televibe `parse_press`, `ticks_after`, `keyboard`, `answered_keyboard`, `offer_answer`, `poll_answer`, `form_answer`, `split_control`.
- Produces:
  ```python
  class App:
      async def on_callback(self, query: CallbackQuery) -> None
      async def on_poll_answer(self, answer: PollAnswer) -> None
  class Agent:
      async def answer(self, uid: int, message: Message, text: str, *, addressed: bool = False,
                       answering: int | None = None) -> None
  ```
  `bot.py` gains `@router.callback_query()` and `@router.poll_answer()`, one line each.

Behaviour to pin:
- **`on_callback`**: chat id and message id come from `query.message` (which may be an `InaccessibleMessage`: never read its text); the person must pass `allowed(chat, query.from_user)`. Unknown message, no control, foreign data → `answer_callback_query` only (Review Focus 2). Otherwise `record_press(..., closes=press.kind in ("offer", "done"))` first, then `answer_callback_query`, then:
  - a tick on an open form → `edit_message_reply_markup(keyboard(form, CONTROL_TEXTS, ticks_after(form, presses)))`, no turn;
  - an offer or [Готово] that closed the control → `edit_message_reply_markup(answered_keyboard(CONTROL_TEXTS.answered.format(label=...)))` (the offer's label, or «Готово»), then a turn: `answerer.answer(uid, <bot message from entry.raw>, offer_answer(...) | form_answer(form, ticks, CONTROL_TEXTS), answering=message_id)`;
  - anything on a closed control → nothing more.
- **`on_poll_answer`**: the uid is `answer.user.id`, accepted only if it is in `allowed_user_ids` (a private chat's id is the user's); `record_vote(..., closes=bool(answer.option_ids))`; when it closed the poll → `stop_poll(chat_id=uid, message_id=...)` and a turn with `poll_answer(poll, answer.option_ids, CONTROL_TEXTS)`, `answering=` the poll message.
- **A text or voice reply to a message with an open control** (`on_message` after the commit, and `on_transcript`): `close_control(id, "text", at)`; a poll is stopped, an offer's or form's keyboard becomes `answered_keyboard(CONTROL_TEXTS.answered_in_text)`; for a form the question text becomes `form_answer(form, ticks, CONTROL_TEXTS, typed=<the text>)`. The turn is the ordinary reply turn.
- **`Agent.answer(..., answering=id)`**: the replied text and author come from the entry `answering` (the bot's answer or poll), not from `message.reply_to_message`; the chain is `await chains.of(uid, answering) or chains.new()`; the presenter's message is the bot message itself (its 👀 is best-effort, REQ-PRESENT-7). Quota, timeout and the editor as for any question (REQ-TURN-8).

- [ ] **Step 1: Write the failing tests** (in `tests/test_app_controls.py`, with a recording answerer as `tests/test_app.py` uses)

```python
async def test_offer_press_is_logged_then_answered_then_asked(app):
    """REQ-TRIGGER-6, REQ-LOG-1: pressed+answered committed before the callback answer; [✓ Да]; one turn."""
    ...  # bot entry 500 with an offer control; on_callback(callback(data="tv:o", message_id=500))
    assert app.bot.names()[:2] == ["answer_callback_query", "edit_message_reply_markup"]
    assert answerer.calls == [(7, 500, "Ответ кнопкой: Да", 500)]          # (uid, message id, text, answering)

async def test_double_press_starts_one_turn(app):
    """REQ-TRIGGER-6: Review Focus 1."""

async def test_press_by_a_stranger_or_on_an_unknown_message_only_stops_the_spinner(app):
    """REQ-TRIGGER-6: Review Focus 2 — answer_callback_query, no events, no turn."""

async def test_ticks_move_without_a_turn_and_survive_a_restart(app, tmp_path):
    """REQ-TRIGGER-6: ticks edit the keyboard; a new App on the same data dir submits them with [Готово]."""
    ...  # tick tv:t:0:1, tv:t:1:0; close app; new App; tv:d →
    assert answerer.calls[-1][2] == "Анкета: вы; он"                       # Review Focus 5

async def test_vote_stops_the_poll_and_asks(app):
    """REQ-TRIGGER-6: a vote → stop_poll and a turn with the chosen options; a retraction starts nothing."""

async def test_text_reply_to_a_form_carries_the_ticks(app):
    """REQ-TRIGGER-7: «Максим» replying to the form → «Анкета: вы; не выбрано\\nТекстом: Максим», control closed."""

async def test_voice_reply_to_a_form_carries_the_ticks(app):
    """REQ-TRIGGER-7: the transcript path does the same (Review Focus 4)."""
```

and in `tests/test_agent.py`:

```python
async def test_a_press_turn_quotes_the_answer_and_continues_its_chain(rig):
    """REQ-TRIGGER-6, REQ-TURN-3: answering=<answer id> → the instructions quote the bot's answer, the session resumes."""
```

and in `tests/test_cli.py` an assertion that `allowed_updates` holds `callback_query` and `poll_answer`.

- [ ] **Step 2: Run** them — FAIL.
- [ ] **Step 3: Implement** the handlers, the reply settling (one private helper `App._settle_reply(uid, reply_to, text) -> str` used by `on_message` and `on_transcript`), `Agent.answer(answering=...)`, the wiring.
- [ ] **Step 4: Amend `SPEC.md`**: REQ-TRIGGER-6 (a press on [Да] or [Готово] or a vote is a question turn on the answered bot message: logged first, then the callback answered, then the turn; a tick is no turn; a closed control starts nothing; only allowed people), REQ-TRIGGER-7 (a text or voice reply to a message with an open control closes it; a form's ticks go with the text); the `allowed_updates` line in §10 if it is listed there.
- [ ] **Step 5: Run** the full suite, ruff, ty — pass.
- [ ] **Step 6: Commit** `git commit -m "A press, a vote or a reply to a control is the person's answer (REQ-TRIGGER-6, 7)"`.

### Task 4: The contract — one question per answer, asked with a control

**Files:**
- Modify: `agent/CLAUDE.md` («Ответы на вопросы», «Знакомство», «Напоминания»), `telegrind/editor.py` (`SYSTEM`), `SPEC.md` §8 (REQ-CONTRACT-16, 17 amended, REQ-CONTRACT-20 new) and §9 (REQ-REMIND-5 amended), `tests/test_live_gate.py`
- Test: `tests/test_agent.py` (a contract text check), the live gate

Content to write (Russian, in the contract's own voice):
- **«Ответы на вопросы»:** at most one question to the person in an answer; the rest waits for later turns. How to ask: an offer you can just do — end with a block `offer` (one button «Да»; not pressing is «нет», so no «нет» button); a question whose «нет» matters, or a choice from a list — a block `poll` (`poll multiple` when several may be right), with the negative answer and, where it fits, «не помню» / «пропустить» among the options, context for the options in `>` lines; several independent short choices — a block `form`. The block is the last thing in the answer; the syntax exactly as televibe's SPEC CONTROL section (copy its three examples). A text reply is always fine. What the person's reply looks like when it comes back: «Ответ кнопкой: …», «Выбрано в опросе: …», «Анкета: …; …» plus «Текстом: …».
- **«Знакомство»:** while there is no «## О человеке», the first-round question is «Как к вам обращаться?» as a `form` with rows `ты | вы` and `он | она`, the name (a name, a name and patronymic, or a nickname) typed as a reply. «вы» until told otherwise; an unticked row or no name is «не сказано». The aside every fifth turn is skipped when the answer already asks something.
- **«Напоминания»:** numbered offers («Напомнить? 1) … 2) …», «1 и 3») are retired: one reminder offer is a block `offer` (label «Напомнить»), several are one `poll multiple` with «ничего» among the options. A pass digest offers in plain words, without a block.
- **`editor.SYSTEM`:** drop the two lines about numbered proposals («вопросы и нумерованные предложения … дословно … он ответит «1 и 3»» and «нумерация — только у вопросов и предложений, которые и в черновике пронумерованы»); keep «каждый вопрос к нему из черновика — обязательно … дословно».

- [ ] **Step 1: Write the failing check** in `tests/test_agent.py`:

```python
def test_contract_asks_with_controls_and_numbered_offers_are_gone():
    """REQ-CONTRACT-20, REQ-REMIND-5: the contract shows all three blocks; no «1 и 3» left in contract or editor."""
    contract = (ROOT / "agent" / "CLAUDE.md").read_text()
    assert all(f"```control\n{kind}" in contract for kind in ("offer", "poll", "form"))
    assert "Как к вам обращаться?" in contract
    assert "1 и 3" not in contract and "1 и 3" not in editor.SYSTEM
```
and a live-gate case `test_live_offer_block` (marked `live`): a question that invites an offer (an entry «Арман вернёт 15000 20 ноября», then the question «что у меня по долгам?») → `split_control(Done.text)` returns a control and the stripped text has no fence. It claims REQ-CONTRACT-20.
- [ ] **Step 2: Run** `uv run pytest tests/test_agent.py -k contract -v` — FAIL.
- [ ] **Step 3: Write** the contract and editor changes above.
- [ ] **Step 4: Amend `SPEC.md`**: REQ-CONTRACT-16 (the form question), REQ-CONTRACT-17 (the aside skipped when the answer already asks), REQ-CONTRACT-20 (one question per answer, asked with a control as above), REQ-REMIND-5 (offers as `offer`/`poll multiple`, no numbering; digests in plain words); §13 gains «a reply keyboard, Mini Apps, quizzes and inline mode» as out of scope.
- [ ] **Step 5: Run** the unit suite, ruff, ty — pass; then the live gate `TELEGRIND_LIVE_HOME=~/.claude-telegrind uv run pytest -m live` — report its result as measured (pass, xfail, or the failure output).
- [ ] **Step 6: Commit** `git commit -m "Contract: one question per answer, asked with [Да], a form or a poll (REQ-CONTRACT-20)"`.

### Task 5: Walkthrough

**Files:**
- Modify: `docs/walkthrough.md`

- [ ] **Step 1: Add steps** after step 12, in the file's Russian style, each with what to check in `log.sqlite`:
  - 14. A question whose answer offers something → [Да] under the answer; press → «✓ Да», events `pressed`+`answered`, Aicha acts; a second tap does nothing.
  - 15. Same, but answer «да, давай» in text → the button becomes «✓ ответ текстом», `closed {"how":"text"}`.
  - 16. A question with a choice → a poll replying to the answer; vote → poll closed, Aicha applies it; retract-and-revote is impossible (closed).
  - 17. Reply to the poll in text instead → poll closed, Aicha answers the text in the same session.
  - 18. With «## О человеке» removed from the person's `CLAUDE.md` (a copy kept), `/start` → «Как к вам обращаться?» with two tick rows; tick, restart the bot (`docker compose restart bot`), reply with the name → «Анкета: …; …\nТекстом: …»; restore the section.
  - 19. Ask something, then ask again in the same chain before pressing → the older [Да] disappears (`closed {"how":"superseded"}`).
- [ ] **Step 2: Commit** `git commit -m "Walkthrough: buttons, forms and polls"`.

### Task 6: Release televibe, pin it, verify

- [ ] **Step 1: Run** the whole telegrind suite against the televibe workspace once more: `uv run pytest -q && uvx ruff check telegrind tests && uvx ruff format --check telegrind tests && uvx ty check` — pass.
- [ ] **Step 2: Ask Maxim for the `v0.7.0` tag.** On his yes: in `~/my/televibe` fast-forward `main` to `work/controls`, push, `git tag v0.7.0 && git push origin v0.7.0`; wait for the PyPI publish to show 0.7.0.
- [ ] **Step 3: Pin** in `pyproject.toml`: remove the `[tool.uv.sources]` televibe entry, `"televibe[telegram]>=0.7,<0.8"`; `uv lock && uv sync -p /usr/bin/python3.12`; run the suite again — pass.
- [ ] **Step 4: Commit** `git commit -m "televibe 0.7 from PyPI"`, fast-forward `main` to the branch from the main checkout, push. Deploying to latitude (`git pull --ff-only && docker compose up -d --build` in `~/my/telegrind3`) and the walkthrough steps 14–19 are run with Maxim, not by this plan.
