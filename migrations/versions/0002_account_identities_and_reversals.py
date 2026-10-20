"""Add linked account identities and reversible transaction metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0002_account_identities"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "accounts" not in tables:
        return

    account_columns = {column["name"] for column in inspector.get_columns("accounts")}
    identity_table_exists = "account_identities" in tables
    if not identity_table_exists:
        op.create_table(
            "account_identities",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("provider", sa.String(length=16), nullable=False),
            sa.Column("identity_value", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("provider IN ('steam', 'discord')", name="ck_account_identities_provider"),
            sa.UniqueConstraint("provider", "identity_value", name="uq_account_identities_provider_value"),
            sa.UniqueConstraint("account_id", "provider", name="uq_account_identities_account_provider"),
        )
        op.create_index("ix_account_identities_account_id", "account_identities", ["account_id"])
    else:
        identity_constraints = inspector.get_unique_constraints("account_identities")
        unique_columns = {tuple(item["column_names"]) for item in identity_constraints}
        if ("provider", "identity_value") not in unique_columns:
            op.create_unique_constraint(
                "uq_account_identities_provider_value",
                "account_identities",
                ["provider", "identity_value"],
            )
        if ("account_id", "provider") not in unique_columns:
            op.create_unique_constraint(
                "uq_account_identities_account_provider",
                "account_identities",
                ["account_id", "provider"],
            )
        check_constraints = inspector.get_check_constraints("account_identities")
        if not any(
            "provider" in (item.get("sqltext") or "").lower()
            and "discord" in (item.get("sqltext") or "").lower()
            and "steam" in (item.get("sqltext") or "").lower()
            for item in check_constraints
        ):
            op.create_check_constraint(
                "ck_account_identities_provider",
                "account_identities",
                "provider IN ('steam', 'discord')",
            )
        index_names = {item["name"] for item in inspector.get_indexes("account_identities")}
        if "ix_account_identities_account_id" not in index_names:
            op.create_index("ix_account_identities_account_id", "account_identities", ["account_id"])

    if "external_user_id" in account_columns:
        op.execute(
            sa.text(
                "INSERT INTO account_identities (account_id, provider, identity_value) "
                "SELECT id, 'discord', CAST(external_user_id AS VARCHAR(32)) FROM accounts "
                "ON CONFLICT DO NOTHING"
            )
        )

    if "system_key" not in account_columns:
        op.add_column("accounts", sa.Column("system_key", sa.String(length=64), nullable=True))
    account_unique_constraints = inspector.get_unique_constraints("accounts")
    if not any(item.get("column_names") == ["system_key"] for item in account_unique_constraints):
        op.create_unique_constraint("uq_accounts_system_key", "accounts", ["system_key"])

    for constraint in inspector.get_check_constraints("accounts"):
        expression = "".join((constraint.get("sqltext") or "").lower().split()).replace("(", "").replace(")", "")
        if expression in {"money>=0", "credits>=0"} and constraint.get("name"):
            op.drop_constraint(constraint["name"], "accounts", type_="check")
    current_checks = {
        "".join((constraint.get("sqltext") or "").lower().split()).replace("(", "").replace(")", "")
        for constraint in sa.inspect(bind).get_check_constraints("accounts")
    }
    if "account_type='system'ormoney>=0" not in current_checks:
        op.create_check_constraint(
            "ck_accounts_money_nonnegative_for_players",
            "accounts",
            "account_type = 'system' OR money >= 0",
        )
    if "account_type='system'orcredits>=0" not in current_checks:
        op.create_check_constraint(
            "ck_accounts_credits_nonnegative_for_players",
            "accounts",
            "account_type = 'system' OR credits >= 0",
        )
    transaction_columns = (
        {column["name"] for column in inspector.get_columns("transactions")}
        if "transactions" in tables
        else set()
    )
    if "transactions" in tables and "actor_account_id" not in transaction_columns:
        op.add_column(
            "transactions",
            sa.Column("actor_account_id", sa.BigInteger(), nullable=True),
        )
        op.create_foreign_key(
            "fk_transactions_actor_account_id_accounts",
            "transactions",
            "accounts",
            ["actor_account_id"],
            ["id"],
        )
        if "actor_id" in transaction_columns and "external_user_id" in account_columns:
            op.execute(
                sa.text(
                    "UPDATE transactions AS t SET actor_account_id = a.id "
                    "FROM accounts AS a WHERE t.actor_id = a.external_user_id"
                )
            )
    if "transactions" in tables:
        transaction_columns = {column["name"] for column in sa.inspect(bind).get_columns("transactions")}
        if "actor_label" not in transaction_columns:
            op.add_column("transactions", sa.Column("actor_label", sa.String(length=255), nullable=True))
        if "reversal_of_transaction_id" not in transaction_columns:
            op.add_column(
                "transactions",
                sa.Column(
                    "reversal_of_transaction_id",
                    sa.Uuid(),
                    sa.ForeignKey("transactions.id"),
                    nullable=True,
                ),
            )
            op.create_unique_constraint(
                "uq_transactions_reversal_of_transaction_id",
                "transactions",
                ["reversal_of_transaction_id"],
            )
        if "actor_id" in transaction_columns:
            op.drop_column("transactions", "actor_id")

    if "game_settlements" in tables:
        game_columns = {column["name"] for column in sa.inspect(bind).get_columns("game_settlements")}
        if "created_by_account_id" not in game_columns:
            op.add_column(
                "game_settlements",
                sa.Column("created_by_account_id", sa.BigInteger(), nullable=True),
            )
            op.create_foreign_key(
                "fk_game_settlements_created_by_account_id_accounts",
                "game_settlements",
                "accounts",
                ["created_by_account_id"],
                ["id"],
            )
            if "created_by" in game_columns and "external_user_id" in account_columns:
                op.execute(
                    sa.text(
                        "UPDATE game_settlements AS g SET created_by_account_id = a.id "
                        "FROM accounts AS a WHERE g.created_by = a.external_user_id"
                    )
                )
        if "created_by" in game_columns:
            op.drop_column("game_settlements", "created_by")

    if "external_user_id" in account_columns:
        op.drop_column("accounts", "external_user_id")

    op.execute(
        sa.text(
            "INSERT INTO accounts (account_type, system_key, money, credits) "
            "VALUES ('system', 'external_money', 0, 0), "
            "('system', 'external_credits', 0, 0) "
            "ON CONFLICT (system_key) DO NOTHING"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "This identity migration is intentionally irreversible; restore from a backup to downgrade."
    )
