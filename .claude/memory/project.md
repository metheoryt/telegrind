# Project memory — telegrind

Durable facts about *this repo* that the code and git history do not state.
One bullet per fact, under a topical heading. No secrets.

## Product invariants

- **"Ничего из написанного не теряется"** is the product claim, not a slogan:
  every handler writes the message row to Postgres *unconditionally*, and only
  then projects to Sheets if a workbook is linked. Declining to extract
  (unknown command, voice, `??`) is never licence to drop the message.
- **The workbook is an optional projection**, not the store — since
  `ef80f31` (2026-09). There is no onboarding gate and no FSM; `/link` is a
  command the user reaches for, not a wall they pass.

## Telegram Bot API — decisions already made (see `docs/telegram-bot-api.md`)

- **Checklists cannot be the wishlist UI.** `sendChecklist` /
  `editMessageChecklist` require `business_connection_id: str`, not optional.
  Dead for a plain bot — do not re-propose.
- **The Bot API does not transcribe voice notes.** Phase 3 (`/retranscribe`)
  needs an external ASR; Telegram contributes `getFile` only, capped at 20 MB.
- **Registering a handler is what subscribes to an update type.** aiogram
  derives `allowed_updates` from the observers that have handlers, so reading
  users' reactions back requires a `message_reaction` handler — there is no
  separate config knob.
- **A bot DOES receive `message_reaction` in a private chat.** The API docs say
  "the bot must be an administrator in the chat"; in a private chat that
  condition is vacuous. Measured 2026-09-11 against `@assinstantbot`: setting a
  reaction gave `old_reaction: [] → new_reaction: [emoji]`, removing it gave a
  separate update with an empty `new_reaction`. So reaction-driven UX is
  reachable, and removal is observable too — do not re-derive this from the
  docs, they do not settle it.
- **Message deletion cannot be observed at all.** The only deletion update is
  `deleted_business_messages`, and `getMe` reports `can_connect_to_business:
  false` for this bot. Dead twice over.
- Pinned aiogram 3.27.0 = Bot API 9.6 (verified 2026-09-10). Bot API 10.x
  features (Rich Messages, ephemeral, guest mode) need a bump to 3.31.

## Known drift

- The `## Architecture` section of the root `CLAUDE.md` is stale as of
  2026-09-10: it still describes the `Outcome`/`Loan`/`Wish` `Sheet`
  subclasses and the `/start` onboarding FSM that `ef80f31` removed. The
  current shape is `llm.extract` → `projection.apply_changes` over
  `store`-owned `Message`/`Fact` rows.
