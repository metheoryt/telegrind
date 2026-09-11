"""dialogue first

The workbook is gone. `message` gains extraction state so a deferred batch
pass can be idempotent, `chat` gains the settings the `_config` worksheet
used to hold, and `fact` is recreated without its spreadsheet coordinates.

DESTRUCTIVE, DELIBERATELY: this drops every existing `fact` row. Facts are
a derivation of messages and come back on the first extraction pass. The
`message` table — the thing the product promises never to lose — is not
touched.

Revision ID: 20ddace46959
Revises: f8ba900863f4
Create Date: 2026-09-11 07:18:28.364573

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20ddace46959"
down_revision: str | Sequence[str] | None = "f8ba900863f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "chat",
        sa.Column("tz_offset", sa.Integer(), server_default="6", nullable=False),
    )
    op.add_column(
        "chat",
        sa.Column("currency", sa.String(), server_default="KZT", nullable=False),
    )

    op.add_column(
        "message", sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("message", sa.Column("extract_model", sa.String(), nullable=True))
    op.add_column(
        "message", sa.Column("extract_prompt_version", sa.String(), nullable=True)
    )
    op.add_column(
        "message",
        sa.Column("extractable", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column("message", sa.Column("extract_error", sa.String(), nullable=True))

    # Drop and recreate rather than alter: every column that carried meaning
    # here was a spreadsheet coordinate, and the rows are a derivation.
    op.drop_table("fact")
    op.create_table(
        "fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_pk", sa.Integer(), nullable=False),
        sa.Column("message_pk", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_pk"], ["chat.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_pk"], ["message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial, not total: a tombstoned fact keeps its (message_pk, seq), so a
    # total constraint would collide with the row that replaces it.
    op.create_index(
        "uq_fact_message_pk_seq_live",
        "fact",
        ["message_pk", "seq"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_fact_chat_kind_at_live",
        "fact",
        ["chat_pk", "kind", "at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_fact_fields", "fact", ["fields"], postgresql_using="gin")


def downgrade() -> None:
    """Downgrade schema.

    The old `fact` table comes back empty. The rows the upgrade dropped are
    not recoverable here — they were a derivation, and re-extraction is what
    restores them, not this function.
    """
    op.drop_index("ix_fact_fields", table_name="fact")
    op.drop_index("ix_fact_chat_kind_at_live", table_name="fact")
    op.drop_index("uq_fact_message_pk_seq_live", table_name="fact")
    op.drop_table("fact")
    op.create_table(
        "fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_pk", sa.Integer(), nullable=False),
        sa.Column("message_pk", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("worksheet", sa.String(), nullable=False),
        sa.Column("sheet_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["chat_pk"], ["chat.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_pk"], ["message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_pk", "worksheet", "sheet_key", name="uq_fact_chat_pk_worksheet_key"
        ),
        sa.UniqueConstraint("message_pk", "seq", name="uq_fact_message_pk_seq"),
    )

    op.drop_column("message", "extract_error")
    op.drop_column("message", "extractable")
    op.drop_column("message", "extract_prompt_version")
    op.drop_column("message", "extract_model")
    op.drop_column("message", "extracted_at")

    op.drop_column("chat", "currency")
    op.drop_column("chat", "tz_offset")
