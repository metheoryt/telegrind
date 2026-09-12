"""How the meta layer is configured. One bot's worth, read from env.

Per-bot configuration is deferred by the design, but the coupling is not:
the token, the working directory and the allowlist are values here rather
than constants, so splitting this module out later is deleting a wiring
file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MetaConfig:
    """The admin allowlist, and where `claude` lives.

    The bot has no sender gate: populate_chat_data resolves — or creates —
    a Chat row for whatever chat_id arrives. That is harmless while the bot
    only records and counts, and stops being harmless the moment text from
    a chat can commit, push and restart production. So the gate is here,
    and it is on the chat rather than on the verdict: `talk` from a stranger
    is not answered at all.
    """

    admin_chat_ids: frozenset[int]
    binary: str = "claude"
    cwd: str = "."
    #: Seconds. A crashed or hung turn breaks the promise 👀 made, so the
    #: turn is bounded and the failure is said out loud.
    timeout: float = 600.0

    def allows(self, chat_id: int) -> bool:
        return chat_id in self.admin_chat_ids

    @classmethod
    def from_env(cls) -> MetaConfig | None:
        """None when CLAUDE_ADMIN_CHAT_IDS is unset or empty.

        None is a supported state, not a misconfiguration: the recording
        half runs unchanged without it, and the dev container has no
        `claude` binary by design.
        """
        raw = os.getenv("CLAUDE_ADMIN_CHAT_IDS", "").strip()
        ids = frozenset(int(part) for part in raw.replace(",", " ").split() if part)
        if not ids:
            return None
        return cls(
            admin_chat_ids=ids,
            binary=os.getenv("CLAUDE_BIN", "claude"),
            # `or`, not a default: .env.dist ships the key with an empty
            # value, and getenv's default never fires for a set-but-empty
            # var — which would hand create_subprocess_exec cwd="".
            cwd=os.getenv("CLAUDE_CWD") or str(Path.cwd()),
            timeout=float(os.getenv("CLAUDE_TURN_TIMEOUT", "600")),
        )
