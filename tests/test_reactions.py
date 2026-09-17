from datetime import UTC, datetime
from types import SimpleNamespace

from telegrind.bot.handlers.reactions import toggle_delete, wants_delete
from telegrind.models import SOURCE_TELEGRAM, Chat, Entry, Fact, LoggedMessage

AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def event(old: list[str], new: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=3260987, type="private"),
        message_id=1072,
        date=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        old_reaction=[SimpleNamespace(type="emoji", emoji=e) for e in old],
        new_reaction=[SimpleNamespace(type="emoji", emoji=e) for e in new],
        user=SimpleNamespace(id=3260987, username="cyphy"),
    )


def test_adding_any_reaction_deletes() -> None:
    """The picker cannot be narrowed, so the accepted set is widened."""
    assert wants_delete(event([], ["💔"])) is True
    assert wants_delete(event([], ["👍"])) is True


def test_removing_the_reaction_restores() -> None:
    assert wants_delete(event(["💔"], [])) is False


def test_swapping_one_reaction_for_another_still_deletes() -> None:
    assert wants_delete(event(["💔"], ["👍"])) is True


def _entity_of(statement: object) -> type | None:
    try:
        return statement.column_descriptions[0]["entity"]
    except AttributeError, IndexError, KeyError:
        return None


class FakeSession:
    """Two selects: the message, then its entry — plus a fact query that
    actually filters by the `entry_pk` it was given.

    A fake that just popped the next canned answer regardless of the
    argument would pass even if the handler queried the wrong pk — measured
    directly: `toggle_delete` calling `tombstone_facts(session, row.id, ...)`
    still passed every test here until this filtered on the real bind
    parameter. So the fact answer is never trusted blind; it is filtered by
    `Fact.entry_pk`, which is what makes a wrong pk find nothing.
    """

    def __init__(self, answers: list[list[object]]) -> None:
        self.answers = answers
        self.statements: list[object] = []

    def begin(self) -> FakeSession:
        return self

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        rows = self.answers.pop(0) if self.answers else []
        if _entity_of(statement) is Fact:
            wanted = statement.compile().params.get("entry_pk_1")
            rows = [row for row in rows if row.entry_pk == wanted]
        return SimpleNamespace(
            scalars=lambda: rows,
            scalar_one_or_none=lambda: rows[0] if rows else None,
        )


async def test_a_reaction_tombstones_the_facts_of_the_message_s_entry() -> None:
    """The gesture now walks message → entry → facts, and the middle hop is
    where it can go quiet: a missed entry tombstones nothing and reports
    nothing, and the bubble on screen still says the facts are gone."""
    msg = LoggedMessage(id=7, chat_pk=1, message_id=1072, kind="text", raw={})
    ent = Entry(
        id=11,
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id="1072",
        message_pk=7,
        occurred_at=AT,
    )
    live = [Fact(chat_pk=1, entry_pk=11, seq=1, kind="expense", at=AT, fields={})]
    session = FakeSession([[msg], [ent], live])

    await toggle_delete(event([], ["💔"]), Chat(id=1, chat_id=3260987), session)

    assert live[0].deleted_at is not None


async def test_removing_the_reaction_restores_them_through_the_same_hop() -> None:
    msg = LoggedMessage(id=7, chat_pk=1, message_id=1072, kind="text", raw={})
    ent = Entry(
        id=11,
        chat_pk=1,
        source=SOURCE_TELEGRAM,
        external_id="1072",
        message_pk=7,
        occurred_at=AT,
    )
    dead = [
        Fact(
            chat_pk=1,
            entry_pk=11,
            seq=1,
            kind="expense",
            at=AT,
            fields={},
            deleted_at=AT,
        )
    ]
    session = FakeSession([[msg], [ent], dead])

    await toggle_delete(event(["💔"], []), Chat(id=1, chat_id=3260987), session)

    assert dead[0].deleted_at is None
