"""entries

The unit that yields facts stops being a Telegram message. `entry` carries
the verdict and the extraction state; `message` goes back to being the
verbatim log; `fact` hangs off the entry.

No data is preserved and none is migrated: v2's database is empty — 0 chats,
0 messages, its own volume `telegrind-v2_pgdata` — measured on latitude
2026-09-16, and re-checked immediately before the deploy. v1's database is a
different database on the same host and is untouched.

Revision ID: 60ddbcfebb5e
Revises: 4d60c7b65ad2
Create Date: 2026-09-17 21:54:52.111535

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "60ddbcfebb5e"
down_revision: str | Sequence[str] | None = "4d60c7b65ad2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_pk", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("message_pk", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.String(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("verdict", sa.String(), nullable=False, server_default="fact"),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extract_model", sa.String(), nullable=True),
        sa.Column("extract_prompt_version", sa.String(), nullable=True),
        sa.Column("extract_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chat_pk"], ["chat.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_pk"], ["message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_pk", "source", "external_id", name="uq_entry_chat_source_external"
        ),
    )
    op.create_index(
        "uq_entry_message_pk",
        "entry",
        ["message_pk"],
        unique=True,
        postgresql_where=sa.text("message_pk IS NOT NULL"),
    )
    op.create_index(
        "ix_entry_queue",
        "entry",
        ["chat_pk", "verdict", "extracted_at", "occurred_at"],
    )

    # The log keeps nothing derived.
    op.drop_column("message", "verdict")
    op.drop_column("message", "extracted_at")
    op.drop_column("message", "extract_model")
    op.drop_column("message", "extract_prompt_version")
    op.drop_column("message", "extract_error")

    # Created anew on the new column, never renamed: an index renamed onto a
    # different column guards nothing and fails silently.
    op.drop_index("uq_fact_message_pk_seq_live", table_name="fact")
    op.drop_constraint("fact_message_pk_fkey", "fact", type_="foreignkey")
    op.drop_column("fact", "message_pk")
    op.add_column("fact", sa.Column("entry_pk", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "fact_entry_pk_fkey", "fact", "entry", ["entry_pk"], ["id"], ondelete="CASCADE"
    )
    op.create_index(
        "uq_fact_entry_pk_seq_live",
        "fact",
        ["entry_pk", "seq"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema.

    Structural only. Facts written against an entry cannot be re-pointed at a
    message — a side-loaded entry has none — so the downgrade drops `fact`'s
    rows with the column. It exists to make the revision reversible on an
    empty database, which is the only database it will ever run against.
    """
    op.drop_index("uq_fact_entry_pk_seq_live", table_name="fact")
    op.drop_constraint("fact_entry_pk_fkey", "fact", type_="foreignkey")
    op.drop_column("fact", "entry_pk")
    op.add_column("fact", sa.Column("message_pk", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "fact_message_pk_fkey",
        "fact",
        "message",
        ["message_pk"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_fact_message_pk_seq_live",
        "fact",
        ["message_pk", "seq"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.add_column(
        "message",
        sa.Column("verdict", sa.String(), nullable=False, server_default="fact"),
    )
    op.add_column(
        "message", sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("message", sa.Column("extract_model", sa.String(), nullable=True))
    op.add_column(
        "message", sa.Column("extract_prompt_version", sa.String(), nullable=True)
    )
    op.add_column("message", sa.Column("extract_error", sa.String(), nullable=True))

    op.drop_index("ix_entry_queue", table_name="entry")
    op.drop_index("uq_entry_message_pk", table_name="entry")
    op.drop_table("entry")
