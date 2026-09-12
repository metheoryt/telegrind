import logging

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegrind.meta import MetaConfig, MetaLayer

from . import meta_wiring
from .dispatcher import dp
from .router import router

log = logging.getLogger(__name__)


def attach_meta(
    async_session: async_sessionmaker[AsyncSession] | None,
) -> MetaLayer | None:
    """Build the conversation half and make it reachable from ingest.

    Off is a supported state rather than a misconfiguration: without
    CLAUDE_ADMIN_CHAT_IDS the recording half runs exactly as before, talk
    is stored and left bare, and a refused question keeps saying so instead
    of going silent. There are two ways to be off and they are logged
    apart — an unset allowlist is a choice, a missing sessionmaker is a
    caller that forgot, and a worker opens its own session so it genuinely
    cannot run without one.

    Separate from `setup_dispatcher` because that function can only run
    once per process — `dp.include_router` on a module-level singleton —
    which would leave the assignment below untestable. It touches no
    router, so a test can call it as often as it likes.
    """
    # Imported here, not at module scope: `setup_dispatcher` owns the order
    # handler modules are first imported in, and a module-level import here
    # would take that away from it.
    from .handlers import handlers

    cfg = MetaConfig.from_env()
    if cfg is None:
        log.info("meta layer off: CLAUDE_ADMIN_CHAT_IDS is not set")
        return None
    if async_session is None:
        log.info("meta layer off: no sessionmaker was passed to setup_dispatcher")
        return None

    layer = MetaLayer(
        cfg,
        async_session=async_session,
        parent_of=meta_wiring.parent_of,
        claim=meta_wiring.claim,
        set_receipt=meta_wiring.set_receipt,
        speak=meta_wiring.speak,
    )
    handlers.HAND_OVER = layer.hand_over
    log.info("meta layer on for %s chat(s)", len(cfg.admin_chat_ids))
    return layer


def setup_dispatcher(
    async_session: async_sessionmaker[AsyncSession] | None = None,
) -> Dispatcher:
    """Import the handler modules, which is what registers them.

    Registration is subscription: importing a handler module registers its
    handlers, and registering an observer is also what subscribes its
    update type — aiogram derives allowed_updates from the handlers that
    exist. So this is the wiring, not a side effect of it.

    Two hazards live in these four lines, and `tests/test_setup.py` pins
    both.

    Order matters *within* an observer, and `handlers` ends in a filterless
    catch-all — so `query` must come first or `ask` never matches. The user
    would still get an answer (`classify.presumed` reads `/q` as a question
    whichever handler stores it), so nothing would look broken: the
    explicit override, the one that exists for when the classifier is
    wrong, would simply be dead code. Three statements rather than one
    `from .handlers import handlers, query`: ruff's isort sorts the names
    inside a single `from` and would put `handlers` in front.

    And `reactions` is not optional despite competing with nothing. aiogram
    derives allowed_updates from the handlers that exist, so dropping that
    import unsubscribes `message_reaction` — which is the delete gesture,
    the only way the user can delete anything.

    There is no ChatActionMiddleware any more: it went with the echo, and
    nothing types.

    The sessionmaker is optional and only the meta layer wants it — a
    worker outlives the update that queued it, so it cannot borrow the
    handler's session. Without one, `attach_meta` says so and the bot runs
    as the recording half alone.
    """
    from . import middleware  # noqa: F401, I001
    from .handlers import query as query
    from .handlers import handlers as handlers
    from .handlers import reactions as reactions

    # After the imports above, never before: this is what hands the ingest
    # handler something to hand a message to, and that module has to exist
    # in the order `setup_dispatcher` chose.
    attach_meta(async_session)

    dp.include_router(router)

    return dp
