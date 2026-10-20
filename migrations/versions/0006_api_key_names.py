"""Store API client names with transaction audit data."""

from alembic import op
import sqlalchemy as sa

revision = "0006_api_key_names"
down_revision = "0005_api_key_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("api_key_name", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transactions", "api_key_name")
