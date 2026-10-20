import hashlib

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import auth
from app.auth import require_read_api_key, require_write_api_key
from app.config import Settings
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


def test_read_only_keys_can_read_but_cannot_write(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        api_clients="reader=read-key,writer=write-key",
        api_read_only_clients="reader",
        _env_file=None,
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    read_key = require_read_api_key("read-key")
    write_key = require_write_api_key("write-key")
    assert (read_key.name, read_key.fingerprint) == ("reader", auth.api_key_fingerprint("read-key"))
    assert (write_key.name, write_key.fingerprint) == ("writer", auth.api_key_fingerprint("write-key"))
    with pytest.raises(HTTPException) as error:
        require_write_api_key("read-key")
    assert error.value.status_code == 403


def test_api_key_fingerprint_is_stable_and_does_not_expose_the_key() -> None:
    key = "private-test-api-key"
    fingerprint = auth.api_key_fingerprint(key)

    assert fingerprint == f"sha256:{hashlib.sha256(key.encode()).hexdigest()}"
    assert key not in fingerprint
    assert len(fingerprint) == 71


def test_read_only_clients_must_be_configured() -> None:
    with pytest.raises(ValidationError):
        Settings(api_clients="writer=write-key", api_read_only_clients="unknown", _env_file=None)


def test_api_clients_are_explicit_name_token_pairs() -> None:
    settings = Settings(api_clients="economy-admin=first-key,casino-bot=second-key", _env_file=None)

    assert settings.api_clients_by_key == {
        "first-key": "economy-admin",
        "second-key": "casino-bot",
    }


@pytest.mark.parametrize(
    ("api_clients",),
    [
        ("malformed",),
        ("=token",),
        ("client=",),
        ("same=first,same=second",),
        ("one=token,two=token",),
        ("invalid name=token",),
    ],
)
def test_invalid_api_client_pairs_are_rejected(api_clients: str) -> None:
    with pytest.raises(ValidationError):
        Settings(api_clients=api_clients, _env_file=None)


def test_read_only_key_is_denied_on_mutation_route(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        api_clients="read-client=read-key",
        api_read_only_clients="read-client",
        _env_file=None,
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    response = TestClient(app).post(
        "/api/v1/accounts/steam/76561198000000000/identities",
        headers={"X-API-Key": "read-key"},
        json={"identity": {"provider": "discord", "value": "123456789012345678"}},
    )

    assert response.status_code == 403


def test_api_routes_enforce_read_or_write_key_permissions() -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        if route.methods == {"GET"}:
            assert require_read_api_key in dependencies, route.path
            assert require_write_api_key not in dependencies, route.path
        else:
            assert require_write_api_key in dependencies, route.path
            assert require_read_api_key not in dependencies, route.path


def test_transaction_model_and_response_include_api_key_identity() -> None:
    assert "api_key_fingerprint" in Transaction.__table__.columns
    assert "api_key_name" in Transaction.__table__.columns
    schema = app.openapi()["components"]["schemas"]["TransactionResponse"]["properties"]
    assert "api_key_fingerprint" in schema
    assert "api_key_name" in schema
