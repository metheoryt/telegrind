# televibe 0.7 — Controls (buttons, forms, polls) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent end its answer with one control block (an offer button, a form of tick buttons, or a poll) that televibe's `Presenter` turns into Telegram controls, and give bots the helpers to read presses and votes back as the person's answer.

**Architecture:** A new module `televibe/telegram/controls.py` holds the control types, the block parser and serializer, the inline keyboards, the callback-data codec, the tick replay and the answer texts. `Presenter` gains one opt-in parameter, `controls: ControlTexts | None`; when it is set, `Done.text` is split, the keyboard rides on the answer and a poll follows it as its own message. `Chains` gains a lookup by message id. televibe keeps no storage of its own: the bot records presses and polls.

**Tech Stack:** Python 3.12+, aiogram `>=3.31,<4` (the `telegram` extra), pytest with `tests/tgkit.py`'s `FakeBot`. Tooling: `uv run pytest`, `uvx ruff check src tests`, `uvx ruff format --check src tests`, `uvx ty check`.

**Spec:** the design `docs/superpowers/specs/2026-10-18-buttons-and-polls-design.md` (telegrind repo), and `~/my/televibe/SPEC.md` §13, which each task amends with its own requirements before the code. This plan lives in telegrind's `docs/superpowers/plans/` because televibe is public and keeps no plans in its history.

**Where the work happens:** a `us:worktrees` workspace of `~/my/televibe`, never its main checkout:

```bash
cd ~/my/televibe && bash ~/my/us/plugins/us/lib/wt.sh create --kind work --key controls --name 'televibe 0.7 controls' --base main --branch work/controls
```

Paths below are relative to that workspace. **Release gate:** the branch is fast-forwarded into `main` and the `v0.7.0` tag (which publishes to PyPI) is pushed only after the telegrind plan's suite passes against this workspace, and only on Maxim's explicit yes.

## Global Constraints

- REQ-SCOPE-2: `dependencies = []` stays empty; `controls.py` imports only the standard library and aiogram.
- REQ-TGPKG-2: `import televibe` must not import `televibe.telegram` or aiogram.
- REQ-TGPKG-4: `televibe.telegram.__all__` is pinned by a test; this plan adds exactly the submodule `controls` to it.
- REQ-TEST-1: every new `REQ-*` id in `SPEC.md` appears in a test name or docstring (`tests/test_requirements.py`).
- Backward compatibility: with `controls=None` (the default) `Presenter` behaves exactly as 0.6.0; every existing test passes unchanged.
- `callback_data` is at most 64 bytes: it carries indices only, never a label (Cyrillic is 2 bytes a character).
- Every callback datum televibe makes starts with `tv:`; any other datum is not televibe's.
- English defaults in `ControlTexts`; a bot replaces them.
- Commits carry no `Co-Authored-By` trailer.

## Review Focus

1. **A block the model wrote badly** (no options, an empty label, a second block, a block mid-text) — the answer text still goes out, with no raw ```` ```control ```` fence visible; a warning is logged.
2. **A poll Telegram rejects** (an option over 100 characters, too many options) — the question still reaches the chat as plain text with numbered options, replying to the answer.
3. **The rich send falls back to plain text** — the keyboard rides on the plain message too, so a control is never lost to the fallback.
4. **A press on a form row that is already ticked** — the tick comes off (a toggle), it does not stay or flip to another option.
5. **Controls off** — a bot that never opted in sends a block-bearing `Done.text` verbatim, as 0.6.0 did.

Each line has its test in the task that owns the code (Tasks 2–4).

---

### Task 1: Probe Telegram live (measure before the API is fixed)

Four facts the design marks *not measured* decide the shape of Tasks 3 and 4. Maxim taps the buttons himself; say so before starting, and run this with him in the session.

**Files:**
- Create (scratchpad, not committed): `$SCRATCH/probe_controls.py`
- Modify (telegrind repo): `.claude/memory/v3.md` (a `## Controls probe (2026-10-18)` section), and the design doc's *Not measured yet* list → *Measured*.

- [ ] **Step 1: Write the probe** — an aiogram script on the **dev** bot (`@assinstantbot`). Read `BOT_TOKEN` from telegrind's `.env` with a two-line parser (never `source .env`; never print the token). Owner chat id: 3260987. It polls `allowed_updates=["message","callback_query","poll_answer","poll"]` and prints each update's `model_dump(mode="json", exclude_none=True)`. It sends, in order:
  1. `send_rich_message` with an inline keyboard of one button `Да` (`callback_data="tv:o"`);
  2. a plain `send_message` with the same keyboard;
  3. `set_message_reaction(👀)` on message 1 (the bot's own message) — print the result or the error;
  4. `send_poll(is_anonymous=False, allows_multiple_answers=True, allow_adding_options=True, description="контекст")` with three options.
  On each `callback_query` it calls `answer_callback_query` then `edit_message_reply_markup` on the pressed message with one button `✓ Да` (`callback_data="tv:x"`), and prints success or the error.
- [ ] **Step 2: Run it with Maxim.** He presses `Да` on 1 and 2, votes in the poll choosing two options, adds an option of his own, then retracts his vote.
- [ ] **Step 3: Record what arrived** in `v3.md`, one bullet per fact, and move the four items in the design doc from *Not measured yet* to a *Measured* list with the date.
- [ ] **Step 4: Apply the fallbacks the results call for** (write the outcome into this plan file before Task 3 starts):
  - Editing the keyboard of a rich message fails → in Task 3, `Presenter` sends an offer's or a form's keyboard on a separate plain message (`send_message`, text = `ControlTexts.form_prompt`), replying to the answer, and that message is `control_message`.
  - A bot cannot react on its own message → nothing changes in code (REQ-PRESENT-7 swallows it); the SPEC line of REQ-PRESENT-11 says the reaction is best-effort there.
  - No `poll_answer` arrives for a non-anonymous poll in a private chat → **stop and tell Maxim**: polls then need replacing by a form, which is a design change.
  - `allow_adding_options`: if the added option shows up in `poll_answer.option_ids` (or a `poll` update carries its text) → `Poll` gets `open: bool` and `send_poll` passes `allow_adding_options=poll.open`; otherwise it is never passed, as Task 3 is written.
- [ ] **Step 5: Commit** the memory and design-doc change in the telegrind workspace: `git commit -m "Controls probe: what Telegram does with keyboards, own reactions and polls"`.

### Task 2: Control types, the block, and its parser

**Files:**
- Create: `src/televibe/telegram/controls.py`
- Modify: `SPEC.md` §13 (a new `### CONTROL` section, REQ-CONTROL-1 and 2)
- Test: `tests/test_tg_controls.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class Offer:
      label: str | None = None            # None: ControlTexts.offer_label
  @dataclass(frozen=True, slots=True)
  class Poll:
      question: str
      options: tuple[str, ...]            # two or more
      multiple: bool = False
      description: str | None = None
  @dataclass(frozen=True, slots=True)
  class Form:
      rows: tuple[tuple[str, ...], ...]   # one or more rows, each two or more options
      ticked: tuple[int | None, ...]      # per row, the option ticked in advance
  type Control = Offer | Poll | Form
  INFO = "control"                        # the fence's info string
  def split_control(text: str) -> tuple[str, Control | None]
  def control_block(control: Control) -> str   # the fenced block, parseable by split_control
  ```

The block syntax (copy into SPEC verbatim):

````text
```control
offer Да
```

```control
poll multiple
? Что из этого займы?
> Переводы людям из выписок за июль–октябрь
- Арман 20 000
- Лолита 50 000
- ничего
```

```control
form
- ты | *вы
- он | она
```
````

`offer` with an optional label; `poll` or `poll multiple`, one `? question`, any `> description` lines (joined by newlines), two or more `- option` lines; `form`, one or more `- a | b | …` rows, a leading `*` marks the option ticked in advance (at most one per row).

- [ ] **Step 1: Write the failing tests**

```python
def test_offer_block_is_split_off():
    """REQ-CONTROL-1: the block at the end becomes a control; the text loses it."""
    text, control = split_control("Слила 31 пару\n\n```control\noffer Да\n```\n")
    assert text == "Слила 31 пару" and control == Offer("Да")

def test_bare_offer_has_no_label():
    assert split_control("x\n```control\noffer\n```")[1] == Offer(None)

def test_poll_and_form_parse():
    """REQ-CONTROL-1: poll question, description, options, multiple; form rows and advance ticks."""
    _, poll = split_control(POLL_TEXT)   # the SPEC example above
    assert poll == Poll("Что из этого займы?", ("Арман 20 000", "Лолита 50 000", "ничего"), True,
                        "Переводы людям из выписок за июль–октябрь")
    _, form = split_control(FORM_TEXT)
    assert form == Form((("ты", "вы"), ("он", "она")), (1, None))

@pytest.mark.parametrize("bad", [
    "```control\npoll\n? q\n- one\n```",                 # one option
    "```control\nform\n- ты\n```",                       # a row of one
    "```control\nform\n- *ты | *вы\n```",                # two ticks in a row
    "```control\noffer Да\n```\n\nmore text",            # not at the end
    "```control\noffer\n```\n```control\noffer\n```",    # two blocks
    "```control\nbutton\n```",                           # unknown kind
])
def test_malformed_block_is_dropped_and_logged(bad, caplog):
    """REQ-CONTROL-1: the text is kept without any control fence, no control, a warning."""
    text, control = split_control("answer\n\n" + bad)
    assert control is None and "```control" not in text and text.startswith("answer")
    assert "control" in caplog.text

def test_text_without_a_block_is_untouched():
    assert split_control("plain ```py\nx\n```") == ("plain ```py\nx\n```", None)

@pytest.mark.parametrize("control", [Offer(None), Offer("Да"), POLL, FORM])
def test_block_round_trips(control):
    """REQ-CONTROL-2: control_block(c) splits back to c."""
    assert split_control("t\n\n" + control_block(control)) == ("t", control)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_tg_controls.py -v` — expect `ImportError` / FAIL.
- [ ] **Step 3: Implement** the types, `split_control` and `control_block` in `controls.py`. A malformed block is removed from the text (every ```` ```control ```` fence and its body), logged with `log.warning("televibe: malformed control block: %s", why)`, and returns `None`. The returned text is right-stripped.
- [ ] **Step 4: Amend `SPEC.md`**: `### CONTROL` under §13 with the syntax above and REQ-CONTROL-1 (split, at most one block, at the end, malformed dropped and logged) and REQ-CONTROL-2 (round trip).
- [ ] **Step 5: Run** `uv run pytest -q && uvx ruff check src tests && uvx ty check` — all pass.
- [ ] **Step 6: Commit** `git commit -m "controls: the control block and its parser (REQ-CONTROL-1, 2)"`.

### Task 3: Keyboards, presses, ticks and answer texts

**Files:**
- Modify: `src/televibe/telegram/controls.py`, `SPEC.md` (REQ-CONTROL-3 to 6)
- Test: `tests/test_tg_controls.py`

**Interfaces:**
- Consumes: Task 2's types.
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class ControlTexts:
      offer_label: str = "Yes"
      done_label: str = "Done"
      pressed: str = "Pressed: {label}"
      voted: str = "Chose in the poll: {options}"
      nothing: str = "nothing"
      form: str = "Form: {rows}"
      unset: str = "not chosen"
      typed: str = "Typed: {text}"
      answered: str = "✓ {label}"
      answered_in_text: str = "✓ answered in text"
      form_prompt: str = "↑"          # text of a separate control message, if Task 1 calls for one
  @dataclass(frozen=True, slots=True)
  class Press:
      kind: Literal["offer", "tick", "done", "inert"]
      row: int | None = None
      option: int | None = None
  def keyboard(control: Offer | Form, texts: ControlTexts, ticks: Sequence[int | None] | None = None) -> InlineKeyboardMarkup
  def answered_keyboard(mark: str) -> InlineKeyboardMarkup     # one inert button
  def parse_press(data: str | None) -> Press | None             # None: not televibe's
  def ticks_after(form: Form, presses: Iterable[Press]) -> tuple[int | None, ...]
  def offer_answer(offer: Offer, texts: ControlTexts) -> str
  def poll_answer(poll: Poll, option_ids: Sequence[int], texts: ControlTexts) -> str
  def form_answer(form: Form, ticks: Sequence[int | None], texts: ControlTexts, typed: str | None = None) -> str
  def poll_fallback_text(poll: Poll) -> str
  ```
- Callback data: offer `tv:o`, tick `tv:t:<row>:<option>`, [Done] `tv:d`, inert `tv:x`.

- [ ] **Step 1: Write the failing tests**

```python
def test_offer_keyboard_is_one_button():
    """REQ-CONTROL-3: one button, the offer's label or the default, data tv:o."""
    [[b]] = keyboard(Offer(None), ControlTexts(offer_label="Да")).inline_keyboard
    assert (b.text, b.callback_data) == ("Да", "tv:o")

def test_form_keyboard_marks_ticks_and_ends_with_done():
    """REQ-CONTROL-3: rows as given, ✓ on the ticked option, [Done] last."""
    kb = keyboard(FORM, ControlTexts(done_label="Готово"), ticks=(1, 0)).inline_keyboard
    assert [[b.text for b in row] for row in kb] == [["ты", "✓ вы"], ["✓ он", "она"], ["Готово"]]
    assert kb[0][1].callback_data == "tv:t:0:1" and kb[2][0].callback_data == "tv:d"

def test_callback_data_fits_64_bytes_for_a_big_form():
    """REQ-CONTROL-3: indices only."""
    big = Form(tuple(tuple("ж" * 60 for _ in range(8)) for _ in range(10)), (None,) * 10)
    for row in keyboard(big, ControlTexts()).inline_keyboard:
        assert all(len(b.callback_data.encode()) <= 64 for b in row)

def test_parse_press():
    """REQ-CONTROL-4: televibe's data parses, anything else is None."""
    assert parse_press("tv:t:2:1") == Press("tick", 2, 1)
    assert parse_press("tv:o") == Press("offer") and parse_press("tv:d") == Press("done")
    assert parse_press("tv:x") == Press("inert")
    assert parse_press("menu:5") is None and parse_press(None) is None and parse_press("tv:t:a") is None

def test_ticks_replay_and_toggle():
    """REQ-CONTROL-5: one tick per row, from the advance ticks; pressing the ticked option unticks it."""
    form = Form((("ты", "вы"), ("он", "она")), (1, None))
    assert ticks_after(form, []) == (1, None)
    assert ticks_after(form, [Press("tick", 0, 0), Press("tick", 1, 1)]) == (0, 1)
    assert ticks_after(form, [Press("tick", 1, 1), Press("tick", 1, 1)]) == (1, None)
    assert ticks_after(form, [Press("tick", 5, 0), Press("offer")]) == (1, None)   # out of range, other kinds: ignored

def test_answer_texts():
    """REQ-CONTROL-6: the person's answer as text, from the bot's templates."""
    t = ControlTexts()
    assert offer_answer(Offer("Слить"), t) == "Pressed: Слить"
    assert offer_answer(Offer(None), t) == "Pressed: Yes"
    assert poll_answer(POLL, [0, 2], t) == "Chose in the poll: Арман 20 000, ничего"
    assert poll_answer(POLL, [], t) == "Chose in the poll: nothing"
    assert form_answer(FORM, (1, None), t, typed="Максим") == "Form: вы; not chosen\nTyped: Максим"

def test_poll_fallback_text_numbers_the_options():
    assert poll_fallback_text(POLL) == (
        "Что из этого займы?\nПереводы людям из выписок за июль–октябрь\n\n1) Арман 20 000\n2) Лолита 50 000\n3) ничего")
```

- [ ] **Step 2: Run** them — FAIL.
- [ ] **Step 3: Implement** the functions above in `controls.py`.
- [ ] **Step 4: Amend `SPEC.md`** with REQ-CONTROL-3 (keyboards, data codec, 64 bytes), REQ-CONTROL-4 (`parse_press`), REQ-CONTROL-5 (`ticks_after`, toggle), REQ-CONTROL-6 (`ControlTexts`, the answer texts, the fallback text).
- [ ] **Step 5: Run** the full suite, ruff, ty — pass.
- [ ] **Step 6: Commit** `git commit -m "controls: keyboards, presses, ticks and answer texts (REQ-CONTROL-3 to 6)"`.

### Task 4: `Presenter` sends controls

**Files:**
- Modify: `src/televibe/telegram/presenter.py`, `tests/tgkit.py` (`FakeBot.send_poll`, `stop_poll`, `edit_message_reply_markup`, `answer_callback_query`; `send_poll` returns a `Message` with a `poll` whose `id` is `f"poll-{message_id}"`), `SPEC.md` (REQ-PRESENT-10 to 12, the PRESENT table)
- Test: `tests/test_tg_presenter.py`

**Interfaces:**
- Consumes: `split_control`, `keyboard`, `poll_fallback_text`, `ControlTexts`.
- Produces:
  - `Presenter(..., controls: ControlTexts | None = None)` — keyword-only, last.
  - After `show()` returns: `presenter.control: Control | None` (what was actually sent as a control) and `presenter.control_message: Message | None` (the message carrying it: the answer for an offer or form, the poll message for a poll). Both `None` when controls are off, when there was no block, or when the control could not be sent as one.
  - `show()` still returns the answer `Message | None` (REQ-PRESENT-2 unchanged).

- [ ] **Step 1: Write the failing tests** (in `tests/test_tg_presenter.py`, using `script(...)` and `FakeBot` as the existing tests do)

```python
OFFER_DONE = Done(SESSION, "Слить 31 пару?\n\n```control\noffer Да\n```", {})

async def test_controls_off_sends_the_block_verbatim():
    """REQ-PRESENT-10: without controls=..., Done.text goes out unchanged and nothing else is sent."""
    bot = FakeBot()
    await Presenter(bot, message()).show(script(Started(SESSION), OFFER_DONE))
    [(_, kw)] = bot.sends()
    assert "```control" in kw["rich_message"].markdown and kw.get("reply_markup") is None

async def test_offer_rides_on_the_answer():
    """REQ-PRESENT-11: the text without the block, the keyboard on the same message."""
    bot = FakeBot()
    p = Presenter(bot, message(), controls=ControlTexts())
    sent = await p.show(script(Started(SESSION), OFFER_DONE))
    [(_, kw)] = bot.sends()
    assert kw["rich_message"].markdown == "Слить 31 пару?"
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "tv:o"
    assert p.control == Offer("Да") and p.control_message is sent

async def test_keyboard_survives_the_plain_fallback():
    """REQ-PRESENT-11, REQ-PRESENT-6: a rejected rich message falls back to plain text, keyboard and all."""
    bot = FakeBot(fail={"send_rich_message": 99})
    await Presenter(bot, message(), controls=ControlTexts()).show(script(Started(SESSION), OFFER_DONE))
    name, kw = bot.sends()[-1]
    assert name == "send_message" and kw["reply_markup"] is not None

async def test_poll_follows_the_answer_as_a_reply():
    """REQ-PRESENT-11: a non-anonymous poll replying to the answer; it is the control message."""
    bot = FakeBot()
    p = Presenter(bot, message(), controls=ControlTexts())
    sent = await p.show(script(Started(SESSION), Done(SESSION, "Вопрос\n\n" + control_block(POLL), {})))
    [(_, kw)] = [c for c in bot.calls if c[0] == "send_poll"]
    assert kw["is_anonymous"] is False and kw["allows_multiple_answers"] is True
    assert kw["reply_parameters"].message_id == sent.message_id and kw["description"] == POLL.description
    assert p.control == POLL and p.control_message.poll.id.startswith("poll-")

async def test_rejected_poll_becomes_text():
    """REQ-PRESENT-11: a poll Telegram refuses still reaches the chat, as numbered text replying to the answer."""
    bot = FakeBot(fail={"send_poll": 99})
    p = Presenter(bot, message(), controls=ControlTexts())
    sent = await p.show(script(Started(SESSION), Done(SESSION, "Вопрос\n\n" + control_block(POLL), {})))
    name, kw = bot.sends()[-1]
    assert kw["text"] == poll_fallback_text(POLL) and kw["reply_parameters"].message_id == sent.message_id
    assert p.control is None and p.control_message is None

async def test_failed_turn_never_shows_a_block():
    """REQ-PRESENT-12: with controls on, a block in Failed.partial is stripped and no control is sent."""
    bot = FakeBot()
    failed = Failed(FailReason.TIMEOUT, SESSION, "", "half\n\n```control\noffer\n```")
    p = Presenter(bot, message(), controls=ControlTexts())
    await p.show(script(Started(SESSION), failed))
    assert "```control" not in bot.sends()[0][1]["rich_message"].markdown and p.control is None

async def test_no_answer_no_poll():
    """REQ-PRESENT-11: when no form of the answer could be sent, the poll is not sent either."""
    bot = FakeBot(fail={"send_rich_message": 99, "send_message": 99})
    await Presenter(bot, message(), controls=ControlTexts()).show(
        script(Started(SESSION), Done(SESSION, "q\n\n" + control_block(POLL), {})))
    assert "send_poll" not in bot.names()
```

- [ ] **Step 2: Run** them — FAIL.
- [ ] **Step 3: Implement** in `presenter.py`: `_finish` splits `Done.text` (and strips `Failed.partial`) when `controls` is set; `_send(text, reply_markup=None)` passes the markup to every attempt; after a sent answer, a `Poll` is sent with `send_poll(chat_id, message_thread_id, question, options=[InputPollOption(text=o) ...], is_anonymous=False, allows_multiple_answers=poll.multiple, description=poll.description, reply_parameters=ReplyParameters(message_id=answer.message_id, allow_sending_without_reply=True))`; any failure → `send_message(poll_fallback_text(poll))` the same way, logged. A poll send waits out flood control like the answer (reuse the retry loop). If Task 1 recorded that a rich message's keyboard cannot be edited, apply its fallback here.
- [ ] **Step 4: Amend `SPEC.md`**: REQ-PRESENT-10 (off by default, verbatim), REQ-PRESENT-11 (offer/form keyboard on every form of the answer; poll after it, non-anonymous, replying to it; poll failure → numbered text; `control` / `control_message`; no answer → no poll), REQ-PRESENT-12 (Failed strips the block); a row in the PRESENT table.
- [ ] **Step 5: Run** the full suite, ruff, ty — pass.
- [ ] **Step 6: Commit** `git commit -m "Presenter: send an offer, a form or a poll (REQ-PRESENT-10 to 12)"`.

### Task 5: Chain lookup by message, package surface, version

**Files:**
- Modify: `src/televibe/telegram/chains.py`, `src/televibe/telegram/__init__.py`, `tests/test_tg_package.py`, `tests/test_tg_chains.py`, `SPEC.md` (REQ-CHAIN-10, REQ-TGPKG-4, §15 out-of-scope list), `README.md` (a short "Controls" section with the block syntax and the press-handling sketch), `pyproject.toml` (`version = "0.7.0"`)

**Interfaces:**
- Produces: `async def Chains.of(self, chat_id: int, message_id: int) -> Chain | None` — the chain holding that message itself (not the one it replies to). `televibe.telegram.__all__` gains `"controls"`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_of_finds_the_chain_of_the_message_itself():
    """REQ-CHAIN-10: a linked bot message resolves to its chain; an unknown one to None."""
    store = MemoryChainStore(); chains = Chains(store)
    await store.link(-1001, 77, "c1")
    assert await chains.of(-1001, 77) == Chain("c1") and await chains.of(-1001, 78) is None
```
and in `test_tg_package.py` the pinned `__all__` gains `"controls"` (REQ-TGPKG-4).

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** `Chains.of`; export `controls` from `televibe/telegram/__init__.py`; bump the version; README section; SPEC §15: remove nothing, add "a reply keyboard and Mini Apps" to the Telegram layer's out-of-scope line.
- [ ] **Step 4: Run** `uv run pytest -q && uvx ruff check src tests && uvx ruff format --check src tests && uvx ty check` — pass; `uv build` succeeds.
- [ ] **Step 5: Commit** `git commit -m "Release 0.7.0: controls — an offer, a form or a poll at the end of an answer (SPEC section 13, CONTROL)"` and push the branch (`git push -u origin work/controls`). **Do not tag.** The tag waits for the telegrind plan (see the release gate above).
