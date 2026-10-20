"""Create the original economy service schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("external_user_id", sa.BigInteger(), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False, server_default="user"),
        sa.Column("money", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credits", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("money >= 0", name="ck_accounts_money"),
        sa.CheckConstraint("credits >= 0", name="ck_accounts_credits"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounts_external_user_id", "accounts", ["external_user_id"], unique=True)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("transaction_type", sa.String(length=64), nullable=False),
        sa.Column("reference_id", sa.String(length=255), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_transactions_idempotency_key"),
    )

    op.create_table(
        "transaction_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("money_delta", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credits_delta", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "money_delta <> 0 OR credits_delta <> 0",
            name="ck_transaction_entries_nonzero_delta",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_entries_account_id", "transaction_entries", ["account_id"])

    op.create_table(
        "game_settlements",
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("game_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("wager_currency", sa.String(length=16), nullable=False),
        sa.Column("wager_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("game_id"),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("game_settlements")
    op.drop_index("ix_transaction_entries_account_id", table_name="transaction_entries")
    op.drop_table("transaction_entries")
    op.drop_table("transactions")
    op.drop_index("ix_accounts_external_user_id", table_name="accounts")
    op.drop_table("accounts")
