# Buttons and polls — design

Status: approved in chat on 2026-10-18, pending the owner's read of this document.
Spans two repos: `televibe` (the Telegram layer, a new release) and `telegrind`
(the bot). `SPEC.md` of each is amended before code, as both repos require.

## Why

Reading the real dialogue with Aicha (`@methybot`, log entries 2630–2733,
17–18 October 2026) showed where typing is the wrong tool:

- **Offers answered with one word.** Aicha ends with «Слить?», «Поменять
  местами?», «Поставить „транспорт“?», and the owner types «да», «да, давай»,
  «да, это он» (2670→2671, 2713→2714, 2725→2726).
- **Choices from a known list.** 17 expenses without a category (2637), «which
  of the 43 transfer recipients owes you» (2647), «он или она» (2639).
- **Batches of questions.** 2715 and 2733 carry three or four numbered
  questions in one message; the owner answers «1. … 2. …» in one text. He
  called this the hardest part of the evening («вопросы пачками это было
  тяжело»).
- **Offers left hanging.** «Могу напоминать раз в месяц, скажи какого числа»
  (2678, 2706), «скажи, если нужно разделить обратно» (2727).

The goal is to UI-ize these moments with Telegram's own controls, and to put
the mechanism in `televibe` so every bot built on it gets it.

## Goals

1. **An offer carries one button, [Да].** The owner presses it, ignores it,
   or answers in text. There is no [Нет]: ignoring an offer is the «no».
2. **A question whose «no» carries a signal is a poll**, not a [Да] button.
   «Is this a loan?», «which category?», «which of these people owe you?» are
   polls whose options include the negative answer.
3. **One question per answer.** Aicha asks at most one question in a reply and
   keeps the rest for later turns. The owner answers; the answer is applied;
   the next question is chosen in the light of it. A batch becomes a series
   by itself. The owner chose this over a prepared queue: «чтобы учитывала
   каждый ответ, пауза не страшна» — a 10–40 s turn per step is accepted.
4. **A press on [Да] or [Готово], or a vote, is the person's reply** to that bot message, as if they
   had typed it: it is logged, and it starts a question turn that continues
   the message's chain.
5. **Text always works.** Any typed or spoken reply to a message with a button
   or a poll is an ordinary reply, handled as today («дай больше контекста»,
   2728).
6. **A small form: rows of tick buttons.** For a question with several
   independent choices (the owner's example: «Как к вам обращаться?» with «ты |
   вы» and «он | она», the name typed), each row is a single choice. A press
   only moves the tick (✓) and wakes nobody; a text reply to the message, or a
   [Готово] button, sends the ticks (and the text) to the agent as one answer.
   The owner chose this over a multiple-choice poll on 2026-10-18; it also fits
   «category + necessity» for an uncategorized expense.

## Non-goals

- A reply keyboard (the persistent menu under the input field), a Mini App,
  quizzes, inline mode. Nothing in the dialogue called for them. A quiz grades
  the answer against a correct option, which is not this use.
- A queue of prepared questions shown without the model.
- Changing how ❤ and 💔 work.

## Anti-goals

- **A question lost to the UI.** If a button or poll cannot be sent, the
  question still reaches the person as text. A turn never ends in silence
  (televibe REQ-PRESENT-6) and neither does a question.
- **A press that leaves no trace in the log.** The log is the record of what
  Telegram sent (telegrind REQ-LOG-1); a callback or a poll answer is part of
  that record and is committed before anything else happens.
- **The editor eating the controls.** The answer editor (REQ-TURN-12) rewrites
  `Done.text`. The control must be taken out before the editor and must never
  be visible to it or to the person as raw markup.

## Constraints and measured facts

From the installed aiogram 3.31 (`Bot.send_rich_message`, `Bot.send_poll`,
`PollAnswer`), read 2026-10-18:

- `send_rich_message` accepts `reply_markup`, so an inline keyboard can ride on
  the answer itself. Inline buttons carry `callback_data` of at most 64 bytes.
- A press arrives as a `callback_query` update, which carries the bot message
  it was pressed on. It must be answered (`answerCallbackQuery`) promptly or the
  client shows a spinner.
- A poll is its own message (`send_poll`), sent as a reply after the answer.
  Question up to 300 characters, options up to 100 each, a `description` up to
  1024 characters (room for context like «COFFEE BOOM 1 400 ₸ + CU MINIMARKET
  580 ₸, 24.09»), `allows_multiple_answers`, `allows_revoting`,
  `allow_adding_options` (the person adds an option of their own; not for
  anonymous polls).
- A vote arrives as a `poll_answer` update carrying only `poll_id`,
  `option_ids`, `option_persistent_ids` and the user — no message. The bot must
  remember which message holds which poll when it sends it. `poll_answer`
  arrives only for non-anonymous polls (`is_anonymous=False`).
- telegrind polls with `allowed_updates=["message", "edited_message",
  "message_reaction"]` (`cli.py`); `callback_query` and `poll_answer` must be
  added.
- televibe ties a turn to an aiogram `Message`: `Presenter(bot, message)` reacts
  on it and replies to it; `Chains.find(message)` follows
  `message.reply_to_message`. REQ-REACTED-2 already lets a message rebuilt from
  stored JSON drive a turn. A press or vote has no message of its own, so its
  turn is driven by the bot message it answers.
- The agent's only channel is `Done.text` (telegrind REQ-CONTRACT-12), so the
  agent declares a control inside its text.

### Measured (2026-10-19, live on the dev bot, private chat)

- A bot may set a reaction on its own message: `set_message_reaction(👀)` on
  the bot's rich message returned `true`.
- A rich message carries an inline keyboard, and `edit_message_reply_markup`
  on it after a press works (it returns the edited `Message`); the same holds
  for a plain message. The `callback_query.message` of a rich message comes
  back with its `rich_message` and `reply_markup`. No separate control message
  is needed.
- A non-anonymous poll in a private chat delivers `poll_answer` with
  `option_ids` (and `option_persistent_ids`, string indices) and the `user`,
  no chat. A retracted vote arrives as a `poll_answer` with empty `option_ids`.
- An option added with `allow_adding_options` arrives two ways: a `poll`
  update listing it (`added_by_user`), and a `poll_option_added` service
  message in the chat that points at the poll message. Adding it is not a
  vote. Polls stay without it: a vote for an added option carries an index
  the stored block cannot name, and «something else» stays a text reply.

## Decided

### How the agent declares a control

At the end of `Done.text` the agent writes one fenced block with a fixed info
string, holding either an offer or a poll. The exact syntax is the builder's
(see below), within these rules:

- At most one block per answer. A second block, or a block anywhere but the end,
  is a malformed control.
- An **offer** names the button's label; the default is «Да».
- A **form** has rows, each a label-less row of two or more single-choice
  options, optionally one option ticked in advance. [Готово] is always added.
- A **poll** has a question, two or more options, whether several may be chosen,
  and an optional description.
- The block is provider-agnostic text: it works for Codex as for Claude.
- A malformed block is dropped from the text and logged; the rest of the answer
  is sent. A poll that Telegram rejects is sent as plain text (question plus
  numbered options), so the question is never lost.

### Split between the repos

**televibe** (a new minor release, `0.7.0`; telegrind pins `>=0.7,<0.8`):

- Parsing: a function that splits an answer into its text and its control
  (none, an offer, a poll). Public, so a bot with an editor can split before
  editing.
- Forms: a press on a form row edits the keyboard to move the tick and starts
  no turn. The ticks are not kept in televibe: the bot derives them from its
  own record of presses (in telegrind, the `pressed` events on the entry) and
  televibe gives a function that renders a form's keyboard from the form and
  its current ticks. [Готово] or a text reply to the form message submits it;
  the receive helper gives the answer text with every row's tick (a row left
  unticked is said so), plus the typed text if any.
- Sending: `Presenter` sends an offer or a form as an inline keyboard on the answer
  message, and a poll as a separate non-anonymous message replying to the
  answer. `show()` must still return the answer message; the builder decides
  how the poll message is returned as well (the bot needs it for its poll
  mapping and its log).
- Receiving: helpers that turn a `callback_query` or a `poll_answer` into what
  the bot needs to run a turn — which bot message was answered, and the
  person's answer as text. The text comes from a `Texts`-style template with
  English defaults; telegrind supplies its own Russian texts, gender-neutral
  since telegrind serves several people («Ответ кнопкой: Да», «Выбрано в
  опросе: подарки, здоровье»).
- Remembering polls: televibe does not own storage. The poll-id → message
  mapping lives in the bot's storage, the way `ChainStore` does. Whether that is
  a new protocol or an addition to `ChainStore` is the builder's call.
- Chains: a turn started by a press or vote continues the chain of the
  answered bot message. That message is itself linked to the chain
  (`chains.link`, REQ-CHAIN-9), so the lookup is by the answered message, not
  by its `reply_to_message`. `Chains` gains a way to find a chain by a message
  id directly.
- `SPEC.md` §13 gets a section for controls; §15's out-of-scope list is checked
  against it.

**telegrind**:

- Wiring: `callback_query` and `poll_answer` handlers in `bot.py` (one line each,
  like the others) and in `allowed_updates`.
- Log: a press (an offer, a form tick, [Готово]) is a `pressed` event and a vote a `voted` event on the **bot's**
  entry that carries the button or the poll, payload the update verbatim,
  committed before the callback is answered or a turn is queued (REQ-LOG-1).
  The poll message the bot sends is stored like any bot message (REQ-LOG-7),
  with its poll id, so a `poll_answer` resolves to it.
- Turn: a press or vote starts a question turn whose prompt is the answer text
  («Ответ кнопкой: Да», «Выбрано в опросе: подарки, здоровье») and whose `instructions` carry the
  answered message's text as a reply does (REQ-TURN-2). It counts against the
  daily quota like any question (REQ-TURN-8) — my call.
- After a press the keyboard is replaced by a mark of what was pressed
  («✓ Да»); after a vote the poll is closed (`stopPoll`), so it cannot be voted
  again. A second press on an already-answered message starts nothing — my call.
- Editor: the control is split off before the editor runs and re-attached after
  it, so the editor never sees it (REQ-TURN-12). The `drafted` event keeps the
  full answer including the block.
- Contract (`agent/CLAUDE.md`, `SPEC.md` §8): one question per answer; an offer
  uses the [Да] block; a question whose «no» matters uses a poll with the
  negative option among its options; when a choice may be «don't remember» or
  «skip», those are options — my call, to be tuned on real turns. The editor's
  rule that keeps every question word for word stays.
- Walkthrough (`docs/walkthrough.md`): a step for an offer pressed, an offer
  answered in text, a poll voted, a poll answered in text.

### Settled after the builder check (my calls, for the owner to check)

- **A control is used up by any answer to it.** A press, a vote, or a typed or
  spoken reply to the message all close it: the keyboard becomes the mark of
  what happened (a text reply marks it as answered in text), the poll is
  stopped. A newer answer in the same chain also closes any control still open
  on an older message of that chain, so a stale [Да] cannot start a repeat of
  work days later. «Answered» is an event on the bot's entry, not a new column.
- **Controls come from question turns only.** A pass digest never carries one:
  the contract says so, and `App.say` strips a block if one appears, so raw
  markup never reaches the chat. (2678 and 2706, the hanging offers, were
  question-turn answers.)
- **A vote's turn hangs on the poll message.** The poll message is linked to
  the answer's chain when sent, so a vote or a text reply to the poll continues
  that chain. Its log entry stores the question and options as its text; the
  turn's `instructions` carry both the poll (question, options, the choice)
  and the answer it replies to. 👀 and the new answer go on the poll message.
- **Contract changes that «one question per answer» brings:**
  - Numbered proposals («Напомнить? 1) … 2) …», `agent/CLAUDE.md` and the
    editor's `SYSTEM`) are retired: several proposals become one
    multiple-choice poll. The editor keeps a question and the control intact.
  - The aside every fifth turn (REQ-CONTRACT-17) is skipped when the answer
    already asks something.
  - Acquaintance (REQ-CONTRACT-16) is one question, «Как к вам обращаться?»
    (the owner's wording, 2026-10-18), sent as a form: a row «ты | вы», a row
    «он | она», and the name — a name, a name and patronymic, or a nickname —
    typed as a reply. Aicha uses «вы» until told otherwise, and takes an
    unticked row or a missing name as not stated.
- **Opt-in in televibe.** `Presenter` parses controls only when the bot turns
  it on, so 0.7 changes nothing for a bot that does not; telegrind turns it on.

## Left to the builder

- The exact block syntax and info string, and the parser's tolerance.
- The televibe API names and shapes (the parse result, the receive helpers,
  the poll store), within the split above.
- How `Presenter` reports the poll message alongside the answer.
- Whether the press mark is an edited keyboard or an edited message.
- What `Reactions` a press-started turn uses, once the reaction-on-own-message
  question is measured.
- Test shape, following each repo's existing suite (no network, no live agent;
  the live gate for an agent that actually emits a block).
