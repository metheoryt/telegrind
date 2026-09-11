from datetime import UTC, datetime
from types import SimpleNamespace

from telegrind.bot.handlers.reactions import wants_delete


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
