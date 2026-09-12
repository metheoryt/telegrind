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


async def release_stranded_turns(
    async_session: async_sessionmaker[AsyncSession] | None,
) -> int:
    """Clear the hand-over markers no live worker can own any more.

    Called from `main.py` between `setup_dispatcher` and `start_polling`,
    and that is the only moment the question has an answer: a worker is a
    bare `asyncio.create_task` in this process, so anything still marked
    before the first update is dispatched was marked by a process that no
    longer exists. Doing it here rather than on shutdown is deliberate —
    the user chose the sweep over a drain, because a drain covers a clean
    stop and this covers Ctrl-C, a `docker compose restart`, the OOM killer
    and a deploy.

    Off is the same two-armed state `attach_meta` reports, deliberately
    re-derived rather than threaded through: nothing here needs the
    `MetaLayer`, `setup_dispatcher`'s signature is pinned by a test that can
    only run once per process, and `MetaConfig.from_env` reads the same
    environment a few lines later in the same boot. Neither arm is logged
    again for that reason — `attach_meta` has already said which one it is.

    It is **process-local**, and that is an assumption rather than a
    guarantee. «No worker can own this» is true of *this* process only:
    a second instance polling the same database — an overlapping deploy,
    or a dev bot pointed at prod — sweeps rows a live worker in the other
    instance is holding, and that worker then answers a message the sweep
    has already re-opened to editing. The harm ceiling is the same single
    duplicate answer as the outage case below, so nothing here defends
    against it; but two bots on one database is not a configuration this
    function is safe under, and nothing else said so.

    It never raises, and `MetaConfig.from_env()` is inside the guard for
    that reason: it calls `int(part)` per token, so a fat-fingered
    `CLAUDE_ADMIN_CHAT_IDS` is a `ValueError` and this promise is restated
    at the call site in `main.py` — two places, so it has to be true of the
    function rather than of the query alone. Nothing reaches it with one
    today (`attach_meta` reads the same variable a few lines earlier and
    goes down first), which is exactly the kind of accident that stops
    being true when the order changes. A bot that will not boot because a
    cleanup query failed is worse than the bug the cleanup exists to fix:
    recording messages is the invariant, and everything here is recovery.
    """
    try:
        cfg = MetaConfig.from_env()
        if cfg is None or async_session is None:
            return 0

        async with async_session() as session:
            released = await meta_wiring.release_hand_overs(session, cfg.admin_chat_ids)
    except Exception:  # recovery, and the bot boots without it
        log.exception("could not release stranded hand-overs")
        return 0

    if released:
        # The message ids, not just the count: the next question is always
        # «which ones», and a restart is exactly when the log is read.
        log.info("released %s stranded hand-over(s): %s", len(released), released)
    return len(released)


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
