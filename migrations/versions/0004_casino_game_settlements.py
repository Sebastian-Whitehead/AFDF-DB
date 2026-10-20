"""Prefix casino-specific game settlement table."""

from alembic import op

revision = "0004_casino_game_settlements"
down_revision = "0003_remove_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("game_settlements", "casino_game_settlements")


def downgrade() -> None:
    op.rename_table("casino_game_settlements", "game_settlements")
