"""receipt emoji

There is no API to read a message's reactions back, so the receipt has to
remember which emoji it last placed. An edit advances it along the cycle,
and that change is how the user sees that the bot noticed the edit.

Existing rows get NULL and are read as carrying the default: it is the only
emoji the previous version ever placed.

Revision ID: 509451c4c7ab
Revises: 20ddace46959
Create Date: 2026-09-11 07:43:35.451797

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "509451c4c7ab"
down_revision: str | Sequence[str] | None = "20ddace46959"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("message", sa.Column("receipt_emoji", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("message", "receipt_emoji")
