"""The receipt cycle (SPEC RECEIPT)."""

from telegrind.receipts import CYCLE, FIRST, next_receipt


def test_cycle_is_hearts_and_wraps():
    """REQ-RECEIPT-2: 💔 → ❤‍🔥 → 💘 → 💔; every one is a heart because every one means the same thing."""
    assert CYCLE == ("💔", "❤‍🔥", "💘") and FIRST == "💔"
    assert next_receipt("💔") == "❤‍🔥" and next_receipt("❤‍🔥") == "💘" and next_receipt("💘") == "💔"
    assert next_receipt(None) == "❤‍🔥"  # an edit of a message whose receipt was never set reads as 💔 → next
    assert next_receipt("👀") == "❤‍🔥"  # an unknown current counts as the first


def test_receipts_are_on_the_bot_api_list():
    """REQ-RECEIPT-1: every receipt is an emoji a bot may set."""
    allowed = set(
        "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()  # noqa: E501, SIM905 - the emoji list is one literal string, as the brief has it
    )
    assert set(CYCLE) <= allowed
