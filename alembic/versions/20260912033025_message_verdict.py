"""message verdict

What routing decided a message is. It replaces `extractable` as the thing the
extraction tail is selected by, and it is never null.

This is the expand half of an expand/contract pair: `extractable` is left in
place and still written, so the downgrade is a plain drop and the rollback
described in the spec stays possible. Dropping `extractable` is a later
contract step, taken by hand once the verdict has held.

Revision ID: 97b074369b9a
Revises: 509451c4c7ab
Create Date: 2026-09-12 03:30:25.016620

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "97b074369b9a"
down_revision: str | Sequence[str] | None = "509451c4c7ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "message",
        sa.Column("verdict", sa.String(), nullable=False, server_default="fact"),
    )
    # Backfill from what the old flag meant. A stored /q was a question and
    # is restored as one; every other unextractable row was a command or a
    # bot reply, which is `system`.
    op.execute(
        """
        UPDATE message SET verdict = CASE
            WHEN extractable THEN 'fact'
            WHEN text LIKE '/q%' THEN 'question'
            ELSE 'system'
        END
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("message", "verdict")
