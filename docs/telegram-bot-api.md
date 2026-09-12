# Telegram Bot API — the surface telegrind can actually use

Curated for this bot, not a copy of the reference. Every claim below was
checked against the *installed* aiogram tree (`.venv/.../aiogram/methods/`,
`/types/`), which is generated from the Bot API schema — so "available" means
callable today, not "exists somewhere in the changelog".

Verified 2026-09-10. Re-verify with:

```console
python -c "import aiogram; print(aiogram.__version__, aiogram.__api_version__)"
ls .venv/lib/python3*/site-packages/aiogram/methods/ | grep <thing>
```

## Version state

| | version | Bot API |
|---|---|---|
| installed (uv.lock) | aiogram 3.27.0 | **9.6** (2026-04-03) |
| latest on PyPI | aiogram 3.31.0 | **10.3** (2026-08-24) |

Anything from 10.0+ (guest mode, Rich Messages, ephemeral messages, live
photos) needs the bump first. aiogram tracks the Bot API within days, so the
lag is ours, not upstream's.

## What telegrind uses today

`sendMessage` (HTML parse mode, set once in `DefaultBotProperties`) ·
`reply` · `edited_message` updates · `reply_to_message` as the edit/delete
affordance · `setMessageReaction` (💩/👌 in `delete_record`) ·
`sendChatAction` via aiogram's `ChatActionMiddleware` + `@flags.chat_action` ·
`sendVideo` with a cached `file_id` (the intro) · `Command` filters ·
long polling.

That is a small slice. The rest of this file is what else is on the table.

## Update types (aiogram observers)

`message`, `edited_message`, `channel_post`, `edited_channel_post`,
`inline_query`, `chosen_inline_result`, `callback_query`, `shipping_query`,
`pre_checkout_query`, `poll`, `poll_answer`, `my_chat_member`, `chat_member`,
`chat_join_request`, `message_reaction`, `message_reaction_count`,
`chat_boost`, `removed_chat_boost`, `business_connection`, `business_message`,
`edited_business_message`, `deleted_business_messages`,
`purchased_paid_media`, `managed_bot`.

**Registration is subscription.** `dp.start_polling()` derives
`allowed_updates` from the observers that actually have handlers
(`Dispatcher.resolve_used_update_types()`). telegrind registers only
`message` + `edited_message`, so *no other update type reaches the process* —
including `message_reaction`. Adding a handler is what turns the tap on; there
is no separate config to remember.

## Available now — ranked by fit with "nothing you write is ever lost"

### 1. `setMyCommands` — the command menu (zero risk, zero users today)

`grep` finds no `set_my_commands` in the repo: `/link`, `/import`, `/rebuild`,
`/reload`, `/unlink` exist only as prose inside `TIP_TEXT`. Registering them
gives the blue **Menu** button, in-client autocomplete, and a description per
command. Scopes (`BotCommandScopeDefault`, `...AllPrivateChats`, per-chat) let
the list differ for a chat with no workbook linked. `setChatMenuButton` swaps
the same button for a Mini App launcher later.

Call it once at startup, next to `setup_dispatcher()`.

### 2. `sendMessageDraft` — stream the extraction instead of faking "typing"

```python
SendMessageDraft(chat_id: int, draft_id: int, text: str,
                 message_thread_id=None, parse_mode=None, entities=None) -> bool
```

Private chats only; `draft_id` must be non-zero, and re-sending the same
`draft_id` **animates** the change client-side. Bot API 9.5 opened it to all
bots (it was business-account-only before), so 3.27 can call it.

Fit: `_ingest` currently shows a generic typing indicator for the whole LLM
round-trip and then dumps `format_records()`. A draft can show the facts as
Claude streams them, then the real `reply` lands. This is the single most
"native" upgrade available — Telegram built it for exactly this shape of bot.

### 3. Reactions as the ack channel

`setMessageReaction` is already in use, so no new surface. Every write
currently costs a reply message; a ✍/👌 reaction on the user's own message
acks silently and keeps the chat readable, with the reply reserved for
"nothing recognised" and errors.

Emoji are a **fixed whitelist** (❤ 👍 👎 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮
💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈
😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊
🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡). Anything else needs Premium/custom emoji and fails
for a bot. Reading a user's reaction back (👍 to confirm, 👎 to re-extract)
needs a `message_reaction` handler — see the subscription note above.

### 4. Inline mode — read your own facts from any chat

`answerInlineQuery` + the `inline_query` observer. Typing `@telegrind_bot
такси` in *any* chat queries Postgres and offers the matching facts as
`InlineQueryResultArticle`s. This is the read side the product lacks: today
the only way to see a fact is the optional Sheets projection.
`InlineQueryResultsButton` can point at a Mini App or `/link` when there is
nothing to show.

### 5. Mini App (`WebAppInfo`, `answerWebAppQuery`, `menu_button_web_app`)

A real alternative to the Google Sheets projection: a page served by the bot,
opened from the menu button, showing facts, totals, and charts. Bigger than
everything above combined — but it is the answer to "I want to see my data"
that does not need a service account, a workbook, or `/rebuild`.

### 6. Business connection — log what he writes *elsewhere*

`business_message` / `edited_business_message` / `deleted_business_messages`
observers plus `business_connection_id` on every send method. A Premium user
connects the bot to their Business account and the bot sees messages in their
own chats. Conceptually the perfect fit for "capture everything I write" —
and heavy: Premium-gated, new consent surface, a whole second identity for
every send. One line of interest, not a plan.

## Needs an aiogram bump (3.27 → 3.31)

- **Rich Messages** (10.1): `sendRichMessage` / `sendRichMessageDraft`,
  `RichBlock*` (paragraph, section heading, table, list, collage, details,
  expandable quotation, **thinking**). Structured output — a fact table
  rendered natively instead of hand-built HTML in `format_records()`.
- **Ephemeral messages** (10.2/10.3, `EphemeralMessageParameters`): group
  messages visible to one user. Only matters if telegrind ever leaves 1:1.
- **Guest mode** (10.0): reply in chats the bot is not a member of.
- 10.3 added `can_stop` / `keep_on_stop` to `sendMessageDraft`.

## Ruled out — don't re-propose

- **Checklists as the wishlist.** `sendChecklist` / `editMessageChecklist`
  require `business_connection_id: str` (not optional). Dead for a plain bot.
- **Voice transcription.** The Bot API does not transcribe. Phase 3 needs an
  external ASR; the API contributes `getFile` and nothing else.
- Gifts, Stars/paid media, live photos, polls, stories, forum topics in
  groups — no line to the product.

## Hard limits and gotchas

- Message text **4096 chars** after entity parsing. `format_records()` over a
  long multi-fact message can exceed it — split, don't truncate.
- `getFile`: **20 MB** download cap; the returned link is valid ≥1 hour
  (re-call `getFile` after that). A local Bot API server lifts the cap.
- `deleteMessage`: only within **48 hours** of sending. Bots may delete their
  own outgoing messages and *incoming* messages in private chats.
- Editing a message the bot sent has no such window, but only
  `editMessageText`/`Caption`/`Media`/`ReplyMarkup` — you cannot turn a text
  message into a photo.
- Broadcast rate: ~30 messages/second overall, ~1/second per chat. Every
  ingest currently costs 1 reply + up to 2 reaction calls.
- `parse_mode` is set globally in `DefaultBotProperties`; any user-supplied
  text interpolated into a reply must be HTML-escaped or it can break the
  message (`format_records()` interpolates fact values).
- Long polling vs webhook: polling is what prod runs and is fine behind the
  homeserver's NAT. A webhook would need an inbound TLS endpoint — not worth
  it for one user.

## Sources

- <https://core.telegram.org/bots/api> — the reference
- <https://core.telegram.org/bots/api-changelog> — version history
- the installed `aiogram` package — the authority on what is callable *here*

## Corrections and additions (2026-09-12)

**"What telegrind uses today", above, describes a bot that no longer exists.**
That section was written 2026-09-10; the workbook layer was deleted and
ingestion rewritten the next day. `format_records`, `delete_record`, `_ingest`,
`TIP_TEXT`, the `sendVideo` intro and the `ChatActionMiddleware` are all gone,
`/q` is the only registered command, and there is no Sheets projection to be
"the only way to see a fact". What telegrind actually calls now is
`setMessageReaction` (💔 / ❤‍🔥 / 💘 as the receipt cycle), `sendMessage` for
`/q` answers, and long polling over `message` + `edited_message` +
`message_reaction`. The rest of this file — version state, update types, the
available-now survey, the bump list, the ruled-out list and the hard limits —
was checked against the installed aiogram tree and still stands.
<!-- src: telegrind 35574a2 | 2026-09-12 -->

### aiogram types and filters

<!-- src: telegrind 35574a2 | 2026-09-12 -->

- **`Message.date` is parsed into a `datetime`; `Message.edit_date` is left a
  raw Unix integer.** Handing it straight to a `timestamptz` column kills the
  whole edit transaction with a `DataError`, so the message keeps its old text
  and its old reaction and the failure reads as "the bot ignored my edit".
  Convert with `datetime.fromtimestamp(raw, tz=UTC)` — Telegram timestamps are
  UTC.
- **`F.text.startswith("/")` resolves to `None` and does not raise when
  `message.text` is `None`** (a voice note, a photo, a sticker). Measured
  directly against aiogram 3.27, contradicting the assumption that it would blow
  up during filter evaluation. A slash catch-all can therefore sit safely ahead
  of media handlers in registration order.
- **But an exception raised *inside* a filter aborts the whole update** before
  any handler runs — no reply, and nothing logged as a handler error. Calling
  `.strip()` on a `None` text inside a marker filter silently killed every
  reply-to-a-photo update. Guard the `None` inside the predicate, never at the
  call site.
- **`reply_to_message_id` is deprecated as of aiogram 3.27** — the installed
  `aiogram/methods/send_message.py` carries `json_schema_extra={"deprecated":
  True}` on the field. Use `ReplyParameters(message_id=...)` on every reply.
- **`dp.message.handlers` is EMPTY when handlers register on a sub-router** the
  dispatcher includes; it lists only handlers bound directly to the dispatcher.
  A verification command that prints it returns a reassuring `[]` that proves
  nothing. Iterate `dp.sub_routers` and read `r.message.handlers`.
- **`managed_bot` / `getManagedBotToken` / `replaceManagedBotToken` are a bot
  *factory* feature** — a bot that creates other bots and fetches their tokens —
  not bot-to-bot collaboration in a chat. Do not reach for them when looking for
  a way to make two runtimes cooperate.

### Bot API semantics worth knowing before designing against them

<!-- src: telegrind 35574a2 | 2026-09-12 -->

- **Telegram includes exactly one level of reply.**
  `message.reply_to_message.reply_to_message` is always `None`. Anything that
  needs the grandparent has to look it up in the bot's own stored rows — which
  is one of the reasons to store the messages the bot itself sends.
- **`MessageOriginHiddenUser` carries no user id**, only a display-name string,
  so "is this forward from me?" is unanswerable for that variant of the
  `forward_origin` union. Any rule branching on a forwarded message's authorship
  needs a third "unknown" arm or it dies on a `None`.
- **Long polling is exclusive, `sendMessage` is not.** Only one process can
  consume a bot's updates, so a second runtime cannot subscribe to the same bot
  — it has to read what the first one persisted. The asymmetry is the trap:
  sending keeps working even when the polling process is dead, so a
  send-only second runtime looks alive while it is deaf. Check the freshness of
  the stored messages before replying.
- **Telegram queues updates a bot did not collect**, so restarting a dead bot
  delivers everything sent while it was down — nothing the user typed is lost.
  The practical consequence: "I sent a message and nothing happened" has two
  indistinguishable causes, a dead process and a raising handler, and only a
  unit test driving the handler tells them apart.

### aiogram defaults and `parse_mode` (2026-09-12)

- **A bot-wide `parse_mode` turns any `<` or `>` in a reply into a lost
  message, and a per-call `parse_mode=None` really does override it.** With
  `Bot(default=DefaultBotProperties(parse_mode="HTML"))` — which is what
  `main.py` constructs — Telegram rejects an unescaped `<` or `>` with a 400
  `can't parse entities` and the whole reply is discarded, which matters most
  for model-authored prose nobody escaped. Passing `parse_mode=None` on the
  call is a genuine override, not a fall-through to the default: in aiogram
  3.27 `BaseSession.prepare_value` returns on `None` before it reaches the
  `Default` branch, and `build_form_data` drops falsy values, so the field is
  simply absent from the request. Verified against the installed tree and
  in-process, never against live Telegram.
  <!-- src: telegrind db9de98 | 2026-09-12 -->
