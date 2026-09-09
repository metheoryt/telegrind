"""initial

Revision ID: 1d25990ec8c1
Revises:
Create Date: 2026-04-24 01:40:13.146867

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d25990ec8c1"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("sheet_url", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("file")
    op.drop_table("chat")
