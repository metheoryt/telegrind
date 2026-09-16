# Editing and redeploying telegrind from the chat — production shape

Status: **superseded 2026-09-16.** Kept as the record of what was decided and
why, because half of it is still the reasoning that matters. Read this block
before anything below it — the body reads as current and is not.

## What replaced it

The premise of this document is that Claude answers from inside telegrind and
therefore has to be able to redeploy the process he is running in. He does not
any more. He is his own bot with his own token and his own process on the host
(`~/my/cladaeb`), and the reasoning is in that repo's
`docs/2026-09-14-design.md` §1: a process cannot rebuild and restart itself.

**Dead, and do not carry forward:**

- *"The bot leaves the compose stack."* It does not. Nothing in telegrind's
  image ever needed a `claude` binary, so telegrind stays an ordinary
  container and the image stays the rollback unit. Rolling back is «run the
  previous build», not git plus alembic.
- *Rule 2 — the deploy must outlive the bot.* A deploy is issued by a
  different process now. There is no `systemctl restart` that kills its own
  caller and no oneshot unit needed to dodge it.
- *Rule 3 — a broken deploy severs the control channel.* It does not. cladaeb
  polls Telegram with its own token and is untouched by telegrind
  crash-looping, so the chat survives the outage it is reporting. The
  deployer's rollback discipline — record the commit and `alembic current`
  before moving, health-check after — is still worth having, but as ordinary
  care rather than as the only way back in.
- *Rule 4 — the deployer reports with its own token.* True by construction now.
- *The gate — one `chat_id`.* Moved to cladaeb, and corrected there: the
  allowlist keys on `from_user.id`, never `chat_id`.

**Survives:** rule 1 — nothing edits the checkout it builds from. `src` is the
build context and is only ever fast-forwarded; Claude edits a separate
worktree, commits and pushes there.

**Still true and still unaddressed:** prod postgres publishes no port. cladaeb
runs on the host and reads telegrind's database, so it needs one on loopback —
the same requirement this document raised, for a different reason.

## What is actually happening instead — decided 2026-09-16

v2 goes up **beside** v1, not over it: a second bot token, a second compose
project, a second database. Both run; the data moves across unhurried; v1 is
stopped only once v2 has everything. That removes the risk this document was
written around — prod is on `2700e0b3a8b6` with only `chat` and `file`, and a
first deploy in place would run five migrations unattended against the live
database at container start. Beside it, v2's `alembic upgrade head` runs
against an empty database, which is the path the test suite already covers.

---

Decided 2026-09-13 in conversation. The meta layer already lets Claude answer
from the chat; this is what has to be true before it can also *change* the bot
and put the change live on latitude.

**The decision: Claude runs as a host process next to the containers, not
inside the image.** The prod image has no `claude` binary, no subscription
login and no `~/.claude`, and baking a personal login into an image is the
thing we are not doing. On the laptop the bot is already a host process — prod
adopts the same shape.

## What that costs, stated once

The bot leaves the compose stack. Postgres stays in it; the bot becomes a
systemd unit on latitude reading the same `.env.prod`. Two consequences:

- **Prod postgres publishes no port today** (verified 2026-09-12), so the host
  bot needs `127.0.0.1:5433` published. Loopback only — never the tailnet.
- **The image stops being the rollback unit.** Rolling back becomes git plus
  alembic rather than «run the previous tag», and that is the reason the
  deployer below has to record a revision before it moves.

## Four rules the naive version breaks

1. **Nothing edits the checkout it builds from.** `src` is the build context
   and is bind-mounted at `/app/local`; it is only ever fast-forwarded. Claude
   edits a separate worktree — its `CLAUDE_CWD` — commits and pushes there.
2. **The deploy must outlive the bot.** `systemctl restart` issued from inside
   the bot kills the process issuing it, and aiogram has already advanced the
   polling offset, so the update is never redelivered: no error, no «готово»,
   nothing. The bot therefore *asks* — `systemctl start telegrind-deploy`, a
   oneshot unit — and the unit does the work.
3. **A broken deploy severs the control channel.** `entrypoint.sh` runs
   `alembic upgrade head` before the process starts, so a bad migration is a
   crash-loop with the schema already moved. No bot means no chat means SSH is
   the only way back. The deployer must therefore roll itself back: record the
   current commit and `alembic current` before moving, health-check after the
   restart, and on failure `alembic downgrade` to the recorded revision, reset
   the code, restart. Code rollback alone does not undo a migration.
4. **The deployer reports with its own token, over Bot API.** The process it
   just restarted cannot tell the user how the restart went.

## The gate

The allowlist is one `chat_id`, and behind it sits `bypassPermissions`, push
rights and the production database. Whoever holds that Telegram account holds
prod. Two things follow: the id must name a private chat, never a group — the
gate is on the chat, so every member of a group would inherit it — and a
deploy is worth a second confirmation in the chat, unlike an answer.

## Prerequisite, not part of this

Prod still runs the 2026-08-01 marvin build: no `message` table, no `fact`, no
`message_reaction` in `allowed_updates`. The dialogue-first code has to be
deployed by hand once before any of this is meaningful. `2700e0b3a8b6` is in
the current migration graph, so that first deploy is a plain
`alembic upgrade head` and not a data migration.

## Open

- Where the agent worktree lives on latitude, and which branch it tracks.
- Whether the bot unit is a user or a system service (the subscription login
  lives in a `$HOME`, which argues for a user unit with lingering enabled).
- What the health check actually asserts. «The process is up» is not enough —
  a bot that starts and fails every update looks healthy to systemd.
