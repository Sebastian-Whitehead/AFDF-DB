import pytest
from pydantic import ValidationError

from app.main import app
from app.models import Account, AccountIdentity, CasinoGameSettlement, Transaction
from app.schemas import IdentityRef, TransactionActor


def test_identity_ids_are_decimal_strings_and_provider_qualified() -> None:
    steam = IdentityRef(provider="steam", value="76561198000000000")
    discord = IdentityRef(provider="discord", value="123456789012345678")

    assert steam.value == "76561198000000000"
    assert discord.value == "123456789012345678"
    with pytest.raises(ValidationError):
        IdentityRef(provider="steam", value="not-a-steam-id")
    with pytest.raises(ValidationError):
        IdentityRef(provider="discord", value="1" * 21)


def test_transaction_actor_requires_exactly_one_actor_kind() -> None:
    TransactionActor(identity=IdentityRef(provider="steam", value="76561198000000000"))
    TransactionActor(label="economy-admin")

    with pytest.raises(ValidationError):
        TransactionActor()
    with pytest.raises(ValidationError):
        TransactionActor(
            identity=IdentityRef(provider="discord", value="123456789012345678"),
            label="economy-admin",
        )


def test_identity_and_reversal_constraints_are_in_the_model() -> None:
    account_columns = set(Account.__table__.columns.keys())
    assert "external_user_id" not in account_columns
    assert "id" in account_columns
    identity_constraints = {
        tuple(constraint.columns.keys())
        for constraint in AccountIdentity.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("provider", "identity_value") in identity_constraints
    assert ("account_id", "provider") in identity_constraints
    assert Transaction.__table__.columns["reversal_of_transaction_id"].unique is True
    assert Account.__table__.columns["system_key"].unique is True
    account_checks = {
        str(constraint.sqltext)
        for constraint in Account.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert any("account_type = 'system' OR money >= 0" in check for check in account_checks)
    assert any("account_type = 'system' OR credits >= 0" in check for check in account_checks)
    assert "settings" not in Account.metadata.tables
    assert "game_settlements" not in Account.metadata.tables
    assert CasinoGameSettlement.__tablename__ == "casino_game_settlements"
    casino_index = next(index for index in Account.__table__.indexes if index.name == "uq_accounts_single_casino")
    assert casino_index.unique is True


def test_openapi_exposes_provider_qualified_account_and_reversal_routes() -> None:
    paths = app.openapi()["paths"]

    assert "/api/v1/accounts/{provider}/{identity}" in paths
    assert "/api/v1/accounts/{provider}/{identity}/identities" in paths
    assert "/api/v1/transactions/{transaction_id}/reverse" in paths


def test_transaction_entries_expose_linked_identity_and_system_clearing_fields() -> None:
    schema = app.openapi()["components"]["schemas"]["TransactionEntryResponse"]["properties"]

    assert {"account_id", "account_type", "system_key", "steam_id", "discord_id",
            "money_delta", "credits_delta"} <= set(schema)
