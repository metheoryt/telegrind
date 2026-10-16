"""Terms Scribe is primed with (SPEC REQ-VOICE-3)."""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_TERMS = 100  # above 100 every request is billed at least 20 s
_BAD = re.compile(r"[<>{}\[\]\\]")


def _ok(term: str) -> bool:
    return 0 < len(term) < 50 and len(term.split()) <= 5 and not _BAD.search(term)


def filter_terms(terms: Iterable[str], limit: int = MAX_TERMS) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        term = term.strip()
        if _ok(term) and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
            if len(out) >= limit:
                break
    return out
