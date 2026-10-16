"""The receipt: a heart on the person's message meaning «stored; tap to delete» (SPEC RECEIPT)."""

CYCLE = ("💔", "❤‍🔥", "💘")
FIRST = CYCLE[0]


def next_receipt(current: str | None) -> str:
    """REQ-RECEIPT-2: the next heart in the cycle; an unknown or missing current counts as the first."""
    index = CYCLE.index(current) if current in CYCLE else 0
    return CYCLE[(index + 1) % len(CYCLE)]
