import anthropic


def test_anthropic_is_a_direct_dependency() -> None:
    major, minor, *_ = (int(p) for p in anthropic.__version__.split("."))
    assert (major, minor) >= (0, 97)


async def test_asyncio_mode_is_auto() -> None:
    """An unmarked async test only runs if asyncio_mode = "auto"."""
    assert True
