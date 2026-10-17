"""The receipt: a heart on the person's message meaning «stored; tap to delete» (SPEC RECEIPT)."""

CYCLE = ("💔", "❤‍🔥", "💘")
FIRST = CYCLE[0]
_VS16 = "\ufe0f"


def holds_heart(emoji: list[str]) -> bool:
    """REQ-REACT-1: one of the person's reactions is a receipt heart, the delete gesture. Plain ❤ is not."""
    hearts = {h.replace(_VS16, "") for h in CYCLE}
    return any(e.replace(_VS16, "") in hearts for e in emoji)


def next_receipt(current: str | None) -> str:
    """REQ-RECEIPT-2: the next heart in the cycle; an unknown or missing current counts as the first."""
    index = CYCLE.index(current) if current in CYCLE else 0
    return CYCLE[(index + 1) % len(CYCLE)]
