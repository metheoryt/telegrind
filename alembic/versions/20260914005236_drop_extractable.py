"""drop extractable

The contract half of the pair `97b074369b9a` opened. `verdict` has selected
the extraction tail since 2026-09-12 and `extractable` has only been written,
never read; this removes the column and its writers.

The downgrade restores it from `verdict`, which is exact rather than
approximate: `fact` is the only verdict the old flag called extractable, and
everything else was one of the three it did not.

Revision ID: 4d60c7b65ad2
Revises: 97b074369b9a
Create Date: 2026-09-14 00:52:36.064696

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4d60c7b65ad2"
down_revision: str | Sequence[str] | None = "97b074369b9a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("message", "extractable")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "message",
        sa.Column("extractable", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.execute("UPDATE message SET extractable = (verdict = 'fact')")
