"""One data directory, one sub-directory per person (SPEC section 2)."""

from __future__ import annotations

import shutil
from pathlib import Path

CONTRACT_SOURCE = Path(__file__).resolve().parent.parent / "agent" / "CLAUDE.md"


class Layout:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.contract = self.data_dir / "CLAUDE.md"
        self.users_dir = self.data_dir / "users"

    def user_dir(self, uid: int) -> Path:
        return self.users_dir / str(uid)

    def log_path(self, uid: int) -> Path:
        return self.user_dir(uid) / "log.sqlite"

    def facts_path(self, uid: int) -> Path:
        return self.user_dir(uid) / "facts.sqlite"

    def media_dir(self, uid: int) -> Path:
        return self.user_dir(uid) / "media"

    def ensure_user(self, uid: int) -> Path:
        """REQ-LAYOUT-1: the directory and media/ on first sight; nothing else."""
        self.media_dir(uid).mkdir(parents=True, exist_ok=True)
        return self.user_dir(uid)

    def install_contract(self, source: Path = CONTRACT_SOURCE) -> None:
        """REQ-LAYOUT-2: overwrite DATA_DIR/CLAUDE.md from the repository's copy."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, self.contract)

    def users(self) -> list[int]:
        if not self.users_dir.is_dir():
            return []
        return sorted(int(p.name) for p in self.users_dir.iterdir() if p.is_dir() and p.name.isdigit())
