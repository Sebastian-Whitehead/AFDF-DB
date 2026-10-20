"""Remove economy-service settings and mark casino on its account row."""

import sqlalchemy as sa
from alembic import op

revision = "0003_remove_settings"
down_revision = "0002_account_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE accounts SET account_type = 'casino' "
            "WHERE id = ("
            "SELECT CAST(value AS BIGINT) FROM settings "
            "WHERE key = 'casino_account_id')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE accounts AS a SET account_type = 'casino' "
            "FROM account_identities AS i, settings AS s "
            "WHERE s.key = 'casino_external_user_id' "
            "AND i.account_id = a.id AND i.provider = 'discord' "
            "AND i.identity_value = s.value"
        )
    )
    op.drop_table("settings")
    op.create_index(
        "uq_accounts_single_casino",
        "accounts",
        ["account_type"],
        unique=True,
        postgresql_where=sa.text("account_type = 'casino'"),
    )


def downgrade() -> None:
    op.drop_index("uq_accounts_single_casino", table_name="accounts")
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
