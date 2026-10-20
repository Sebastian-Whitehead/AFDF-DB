import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import auth
from app.auth import (
    AuthenticatedApiKey,
    require_admin_api_key,
    require_read_api_key,
    require_write_api_key,
)
from app.config import Settings
from app.main import app
from app.models import Account, AccountIdentity, CasinoGameSettlement, Transaction, TransactionEntry
from app.routers.accounts import link_identity, migrate_identity
from app.routers.casino import reservation_matches, settlement_matches
from app.routers.transactions import account_transactions, transfer_matches
from app.services import identity_value, merge_accounts, transaction_responses
from app.schemas import (
    AdjustmentRequest,
    IdentityRef,
    LinkIdentityRequest,
    MigrateIdentityRequest,
    ReserveRequest,
    SettleRequest,
    TransactionActor,
    TransferRequest,
)
from app.routers.accounts import link_identity


def test_identity_ids_are_decimal_strings_and_provider_qualified() -> None:
    steam = IdentityRef(provider="steam", value="76561198000000000")
    discord = IdentityRef(provider="discord", value="123456789012345678")

    assert steam.value == "76561198000000000"
    assert discord.value == "123456789012345678"
    with pytest.raises(ValidationError):
        IdentityRef(provider="steam", value="not-a-steam-id")
    with pytest.raises(ValidationError):
        IdentityRef(provider="discord", value="1" * 21)
    with pytest.raises(ValidationError):
        IdentityRef(provider="steam", value="١٢٣٤٥")
    with pytest.raises(ValidationError):
        IdentityRef(provider="steam", value="12345678901234567")
    with pytest.raises(ValidationError):
        IdentityRef(provider="steam", value="076561198000000000")
    with pytest.raises(ValidationError):
        IdentityRef(provider="discord", value="18446744073709551616")
    with pytest.raises(ValidationError):
        IdentityRef(provider="discord", value="0")
    with pytest.raises(HTTPException) as error:
        identity_value("١٢٣٤٥", "steam")
    assert error.value.status_code == 422


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


def test_adjustment_request_validates_without_game_fields() -> None:
    AdjustmentRequest(
        amount=10,
        reason="test adjustment",
        reference_id="adjustment-1",
        actor={"label": "test"},
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
    assert "/api/v1/accounts/{provider}/{identity}/migrate" in paths
    assert "/api/v1/transactions/{transaction_id}/reverse" in paths
    assert "/api/v1/transfers" in paths


@pytest.mark.parametrize(
    ("provider", "identity", "linked_provider", "linked_value"),
    [
        ("steam", "76561198000000000", "discord", "123456789012345678"),
        ("discord", "123456789012345678", "steam", "76561198000000000"),
    ],
)
async def test_link_identity_supports_both_provider_directions(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    identity: str,
    linked_provider: str,
    linked_value: str,
) -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        add=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    account = Account(id=42, account_type="user")
    monkeypatch.setattr(
        "app.routers.accounts.require_account",
        AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        "app.routers.accounts.find_account",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.routers.accounts.to_account_response",
        AsyncMock(return_value=None),
    )
    await link_identity(
        provider=provider,
        identity=identity,
        request=LinkIdentityRequest(
            identity=IdentityRef(provider=linked_provider, value=linked_value),
        ),
        session=session,
        request_api_key=AuthenticatedApiKey(
            name="account-admin",
            fingerprint="sha256:test",
        ),
    )

    linked_identity = session.add.call_args_list[1].args[0]
    assert isinstance(linked_identity, AccountIdentity)
    assert linked_identity.account_id == account.id
    assert linked_identity.provider == linked_provider
    assert linked_identity.identity_value == linked_value
    audit = session.add.call_args_list[0].args[0]
    assert audit.transaction_type == "identity_link"
    assert audit.api_key_name == "account-admin"
    session.commit.assert_awaited_once()


async def test_linking_existing_identity_merges_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = Account(id=1, account_type="user", money=30, credits=5)
    source = Account(id=2, account_type="user", money=12, credits=7)
    session = SimpleNamespace()
    monkeypatch.setattr(
        "app.routers.accounts.require_account",
        AsyncMock(return_value=destination),
    )
    monkeypatch.setattr(
        "app.routers.accounts.find_account",
        AsyncMock(return_value=source),
    )
    merge = AsyncMock(return_value=destination)
    monkeypatch.setattr("app.routers.accounts.merge_accounts", merge)
    monkeypatch.setattr(
        "app.routers.accounts.to_account_response",
        AsyncMock(return_value=None),
    )

    await link_identity(
        provider="steam",
        identity="76561198000000000",
        request=LinkIdentityRequest(
            identity=IdentityRef(
                provider="discord",
                value="123456789012345678",
            ),
        ),
        session=session,
        request_api_key=AuthenticatedApiKey(
            name="account-admin",
            fingerprint="sha256:test",
        ),
    )

    assert merge.await_args.kwargs["source_id"] == source.id
    assert merge.await_args.kwargs["destination_id"] == destination.id
    assert merge.await_args.kwargs["required_source_identity"] == (
        "discord",
        "123456789012345678",
    )


async def test_migrate_identity_replaces_identity_and_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = Account(id=1, account_type="user", money=40, credits=9)
    identity_row = AccountIdentity(
        id=1,
        account_id=account.id,
        provider="discord",
        identity_value="123456789012345678",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                account,
                None,
                None,
                identity_row,
            ]
        ),
        add=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.routers.accounts.to_account_response",
        AsyncMock(return_value=None),
    )

    await migrate_identity(
        provider="discord",
        identity="123456789012345678",
        request=MigrateIdentityRequest(
            identity=IdentityRef(
                provider="discord",
                value="234567890123456789",
            )
        ),
        session=session,
        request_api_key=AuthenticatedApiKey(
            name="account-admin",
            fingerprint="sha256:test",
        ),
    )

    assert identity_row.identity_value == "234567890123456789"
    audit = session.add.call_args.args[0]
    assert audit.transaction_type == "identity_migration"
    assert audit.idempotency_key == (
        "identity-migration:discord:123456789012345678:234567890123456789"
    )
    session.commit.assert_awaited_once()


async def test_account_merge_combines_balances_and_reassigns_history() -> None:
    source = Account(id=1, account_type="user", money=50, credits=8)
    destination = Account(id=2, account_type="user", money=25, credits=12)
    source_identity = AccountIdentity(
        id=1,
        account_id=source.id,
        provider="discord",
        identity_value="123456789012345678",
    )
    destination_identity = AccountIdentity(
        id=2,
        account_id=destination.id,
        provider="steam",
        identity_value="76561198000000000",
    )
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                ScalarRows([source, destination]),
                ScalarRows([source_identity, destination_identity]),
            ]
        ),
        execute=AsyncMock(),
        delete=AsyncMock(),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    merged = await merge_accounts(
        session,
        source_id=source.id,
        destination_id=destination.id,
        api_key_name="account-admin",
        api_key_fingerprint="sha256:test",
        required_source_identity=("discord", source_identity.identity_value),
        required_destination_identity=("steam", destination_identity.identity_value),
    )

    assert merged is destination
    assert (destination.money, destination.credits) == (75, 20)
    assert session.execute.await_count == 4
    updated_tables = {
        str(call.args[0]).split(" ", 1)[1].split(" ", 1)[0]
        for call in session.execute.await_args_list
    }
    assert updated_tables == {
        "account_identities",
        "transaction_entries",
        "transactions",
        "casino_game_settlements",
    }
    assert session.delete.await_args.args == (source,)
    audit = session.add.call_args.args[0]
    assert audit.transaction_type == "account_merge"
    assert audit.api_key_name == "account-admin"
    session.commit.assert_awaited_once()


async def test_account_merge_rejects_conflicting_provider_ids() -> None:
    source = Account(id=1, account_type="user", money=50, credits=8)
    destination = Account(id=2, account_type="user", money=25, credits=12)
    identities = [
        AccountIdentity(
            id=1,
            account_id=source.id,
            provider="steam",
            identity_value="76561198000000000",
        ),
        AccountIdentity(
            id=2,
            account_id=destination.id,
            provider="steam",
            identity_value="76561198000000001",
        ),
    ]
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                ScalarRows([source, destination]),
                ScalarRows(identities),
            ]
        ),
        execute=AsyncMock(),
    )

    with pytest.raises(HTTPException) as error:
        await merge_accounts(
            session,
            source_id=source.id,
            destination_id=destination.id,
            api_key_name="account-admin",
            api_key_fingerprint="sha256:test",
        )

    assert error.value.status_code == 409
    session.execute.assert_not_awaited()


async def test_account_merge_can_replace_identity_and_audit_migration() -> None:
    source = Account(id=1, account_type="user", money=50, credits=8)
    destination = Account(id=2, account_type="user", money=25, credits=12)
    old_identity = AccountIdentity(
        id=1,
        account_id=source.id,
        provider="discord",
        identity_value="123456789012345678",
    )
    new_identity = AccountIdentity(
        id=2,
        account_id=destination.id,
        provider="discord",
        identity_value="234567890123456789",
    )
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                ScalarRows([source, destination]),
                ScalarRows([old_identity, new_identity]),
            ]
        ),
        execute=AsyncMock(),
        delete=AsyncMock(),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    await merge_accounts(
        session,
        source_id=source.id,
        destination_id=destination.id,
        api_key_name="account-admin",
        api_key_fingerprint="sha256:test",
        discard_identity=("discord", old_identity.identity_value),
        required_source_identity=("discord", old_identity.identity_value),
        required_destination_identity=("discord", new_identity.identity_value),
        audit_idempotency_key="identity-migration:discord:old:new",
    )

    assert session.execute.await_count == 5
    assert session.add.call_args.args[0].transaction_type == "identity_migration"
    assert session.add.call_args.args[0].idempotency_key == (
        "identity-migration:discord:old:new"
    )


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


def test_account_administration_requires_an_admin_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        api_clients="writer=write-key,account-admin=admin-key",
        api_admin_clients="account-admin",
        _env_file=None,
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    admin_key = require_admin_api_key("admin-key")
    assert admin_key.name == "account-admin"
    with pytest.raises(HTTPException) as error:
        require_admin_api_key("write-key")
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


def test_admin_clients_must_be_configured_and_not_read_only() -> None:
    with pytest.raises(ValidationError):
        Settings(
            api_clients="writer=write-key",
            api_admin_clients="unknown",
            _env_file=None,
        )
    with pytest.raises(ValidationError):
        Settings(
            api_clients="reader=read-key",
            api_admin_clients="reader",
            api_read_only_clients="reader",
            _env_file=None,
        )


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
            if route.path.endswith(("/identities", "/migrate")):
                assert require_admin_api_key in dependencies, route.path
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


def test_transfer_and_game_request_constraints() -> None:
    transfer = TransferRequest(
        sender={"provider": "steam", "value": "76561198000000000"},
        recipient={"provider": "discord", "value": "123456789012345678"},
        currency="money",
        amount=10,
        reference_id="payment-123",
    )
    assert transfer.amount == 10
    with pytest.raises(ValidationError):
        TransferRequest(
            sender={"provider": "steam", "value": "76561198000000000"},
            recipient={"provider": "discord", "value": "123456789012345678"},
            currency="money",
            amount=0,
            reference_id="payment-123",
        )
    with pytest.raises(ValidationError):
        ReserveRequest(
            game_type="test",
            currency="money",
            players=[
                {
                    "identity": {"provider": "steam", "value": "76561198000000000"},
                    "amount": 9_223_372_036_854_775_807,
                },
                {
                    "identity": {"provider": "discord", "value": "123456789012345678"},
                    "amount": 1,
                },
            ],
            reference_id="game-1",
            actor={"label": "test"},
        )
    with pytest.raises(ValidationError):
        SettleRequest(
            winner={"provider": "steam", "value": "76561198000000000"},
            result={"payout": 99},
            actor={"label": "test"},
        )


class ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


@pytest.mark.parametrize("currency", ["money", "credits"])
async def test_transfer_retry_matches_exact_ledger_entries(currency: str) -> None:
    transaction_id = uuid4()
    transaction = Transaction(
        id=transaction_id,
        idempotency_key="transfer:ref-1",
        transaction_type=f"{currency}_transfer",
        reference_id="ref-1",
        actor_account_id=1,
    )
    amount = 25
    entries = [
        TransactionEntry(
            transaction_id=transaction_id,
            account_id=1,
            money_delta=-amount if currency == "money" else 0,
            credits_delta=-amount if currency == "credits" else 0,
        ),
        TransactionEntry(
            transaction_id=transaction_id,
            account_id=2,
            money_delta=amount if currency == "money" else 0,
            credits_delta=amount if currency == "credits" else 0,
        ),
    ]
    session = SimpleNamespace(scalars=AsyncMock(return_value=ScalarRows(entries)))

    assert await transfer_matches(
        session,
        transaction,
        sender_id=1,
        recipient_id=2,
        currency=currency,
        amount=amount,
        reference_id="ref-1",
    )
    assert not await transfer_matches(
        session,
        transaction,
        sender_id=1,
        recipient_id=2,
        currency=currency,
        amount=amount + 1,
        reference_id="ref-1",
    )


async def test_game_reservation_retry_compares_players_and_transaction() -> None:
    transaction_id = uuid4()
    transaction = Transaction(
        id=transaction_id,
        idempotency_key="reserve-ref",
        transaction_type="money_wager",
        reference_id="reserve-ref",
    )
    game = CasinoGameSettlement(
        game_id=uuid4(),
        game_type="coinflip",
        status="reserved",
        wager_currency="money",
        wager_amount=30,
        created_by_account_id=1,
    )
    entries = [
        TransactionEntry(transaction_id=transaction_id, account_id=1, money_delta=-10, credits_delta=0),
        TransactionEntry(transaction_id=transaction_id, account_id=2, money_delta=-20, credits_delta=0),
        TransactionEntry(transaction_id=transaction_id, account_id=9, money_delta=30, credits_delta=0),
    ]
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=transaction),
        scalars=AsyncMock(return_value=ScalarRows(entries)),
    )
    args = dict(
        game_type="coinflip",
        currency="money",
        player_amounts={1: 10, 2: 20},
        creator_id=1,
        casino_id=9,
        total=30,
        reference_id="reserve-ref",
    )

    assert await reservation_matches(session, game, **args)
    assert not await reservation_matches(
        session,
        game,
        **{**args, "player_amounts": {1: 15, 2: 15}},
    )


async def test_settlement_retry_requires_same_winner_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    transaction_id = uuid4()
    game = CasinoGameSettlement(
        game_id=uuid4(),
        game_type="coinflip",
        status="settled",
        wager_currency="money",
        wager_amount=20,
        result={"coin": "heads", "fee": 0, "payout": 20},
        transaction_id=transaction_id,
    )
    transaction = Transaction(
        id=transaction_id,
        idempotency_key=f"game-settle:{game.game_id}",
        transaction_type="money_payout",
        reference_id=str(game.game_id),
    )
    entries = [
        TransactionEntry(transaction_id=transaction_id, account_id=1, money_delta=20, credits_delta=0),
        TransactionEntry(transaction_id=transaction_id, account_id=9, money_delta=-20, credits_delta=0),
    ]
    session = SimpleNamespace(
        get=AsyncMock(return_value=transaction),
        scalars=AsyncMock(return_value=ScalarRows(entries)),
    )

    monkeypatch.setattr(
        "app.routers.casino.casino_account",
        AsyncMock(return_value=Account(id=9)),
    )
    assert await settlement_matches(session, game, winner_id=1, result={"coin": "heads"})
    assert not await settlement_matches(session, game, winner_id=2, result={"coin": "heads"})
    assert not await settlement_matches(session, game, winner_id=1, result={"coin": "tails"})


async def test_transaction_responses_batch_enrichment_queries() -> None:
    transaction_id = uuid4()
    transaction = Transaction(
        id=transaction_id,
        idempotency_key="transfer-1",
        transaction_type="money_transfer",
        reference_id="transfer-1",
        actor_account_id=1,
        actor_label=None,
        api_key_name="casino-bot",
        api_key_fingerprint="sha256:test",
        reversal_of_transaction_id=None,
        created_at=datetime.now(timezone.utc),
    )
    entries = [
        TransactionEntry(
            transaction_id=transaction_id,
            account_id=1,
            money_delta=-10,
            credits_delta=0,
        ),
        TransactionEntry(
            transaction_id=transaction_id,
            account_id=2,
            money_delta=10,
            credits_delta=0,
        ),
    ]
    accounts = [
        Account(id=1, account_type="user", system_key=None),
        Account(id=2, account_type="user", system_key=None),
    ]
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                ScalarRows(entries),
                ScalarRows(accounts),
            ]
        ),
        execute=AsyncMock(
            side_effect=[
                ScalarRows(
                    [
                        (1, "steam", "76561198000000000"),
                        (2, "discord", "123456789012345678"),
                    ]
                ),
                ScalarRows([]),
            ]
        ),
    )

    response = (await transaction_responses(session, [transaction]))[0]

    assert response.entries[0].money_delta == -10
    assert response.entries[0].steam_id == "76561198000000000"
    assert response.entries[1].discord_id == "123456789012345678"
    assert session.scalars.await_count == 2
    assert session.execute.await_count == 2


async def test_transaction_history_applies_cursor_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor_id = uuid4()
    cursor = SimpleNamespace(
        id=cursor_id,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    class UniqueRows:
        def unique(self):
            return self

        def all(self):
            return []

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=cursor),
        scalars=AsyncMock(return_value=UniqueRows()),
    )
    monkeypatch.setattr(
        "app.routers.transactions.require_account",
        AsyncMock(return_value=Account(id=1)),
    )
    monkeypatch.setattr(
        "app.routers.transactions.transaction_responses",
        AsyncMock(return_value=[]),
    )

    result = await account_transactions(
        provider="steam",
        identity="76561198000000000",
        limit=50,
        before=cursor_id,
        session=session,
    )

    statement = str(session.scalars.await_args.args[0])
    assert result == []
    assert "transactions.created_at <" in statement
    assert "transactions.id <" in statement
