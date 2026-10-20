"""Record the API key fingerprint for each transaction."""

from alembic import op
import sqlalchemy as sa

revision = "0005_api_key_audit"
down_revision = "0004_casino_game_settlements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("api_key_fingerprint", sa.String(length=71), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transactions", "api_key_fingerprint")
