"""Account identity, balance, exchange, and leaderboard endpoints."""

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    AuthenticatedApiKey,
    require_admin_api_key,
    require_read_api_key,
    require_write_api_key,
)
from ..config import get_settings
from ..db import get_session
from ..models import Account, AccountIdentity, Transaction, TransactionEntry
from ..schemas import (
    AccountResponse,
    AdjustmentRequest,
    ExchangeRequest,
    LeaderboardEntry,
    LeaderboardResponse,
    LinkIdentityRequest,
    MigrateIdentityRequest,
)
from ..services import (
    IdentityProvider,
    account_identities,
    add_system_entry,
    ensure_account,
    find_account,
    identity_value,
    lock_accounts,
    require_account,
    require_system_account,
    merge_accounts,
    to_account_response,
    transaction_actor_fields,
)

router = APIRouter()


@router.get(
    "/api/v1/accounts/{provider}/{identity}",
    response_model=AccountResponse,
    dependencies=[Depends(require_read_api_key)],
)
async def get_account(
    provider: IdentityProvider,
    identity: str,
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    value = identity_value(identity, provider)
    account = await require_account(session, provider, value)
    return await to_account_response(session, account)


@router.put(
    "/api/v1/accounts/{provider}/{identity}",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def ensure_account_route(
    provider: IdentityProvider,
    identity: str,
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    value = identity_value(identity, provider)
    account = await ensure_account(session, provider, value)
    return await to_account_response(session, account)


@router.post(
    "/api/v1/accounts/{provider}/{identity}/identities",
    response_model=AccountResponse,
    dependencies=[Depends(require_admin_api_key)],
)
async def link_identity(
    provider: IdentityProvider,
    identity: str,
    request: LinkIdentityRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_admin_api_key),
) -> AccountResponse:
    account = await require_account(session, provider, identity_value(identity, provider))
    if account.account_type != "user":
        raise HTTPException(status_code=409, detail="Only user accounts can link identities")
    linked_provider = request.identity.provider
    if linked_provider == provider:
        raise HTTPException(
            status_code=422,
            detail="Linked identity must use a different provider",
        )
    linked_value = identity_value(request.identity.value, linked_provider)
    existing = await find_account(session, linked_provider, linked_value)
    if existing is not None:
        if existing.id == account.id:
            return await to_account_response(session, account)
        try:
            merged_account = await merge_accounts(
                session,
                source_id=existing.id,
                destination_id=account.id,
                api_key_name=request_api_key.name,
                api_key_fingerprint=request_api_key.fingerprint,
                required_source_identity=(linked_provider, linked_value),
                required_destination_identity=(provider, identity_value(identity, provider)),
            )
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="ACCOUNT_MERGE_CONFLICT")
        return await to_account_response(session, merged_account)

    current = await session.scalar(
        select(AccountIdentity).where(
            AccountIdentity.account_id == account.id,
            AccountIdentity.provider == linked_provider,
        )
    )
    if current is not None and current.identity_value != linked_value:
        raise HTTPException(
            status_code=409,
            detail="Account already has an identity for this provider",
        )
    if current is None:
        session.add(
            Transaction(
                idempotency_key=f"identity-link:{uuid4()}",
                transaction_type="identity_link",
                reference_id=(
                    f"identity-link:{provider}:{identity}:"
                    f"{linked_provider}:{linked_value}"
                ),
                description=(
                    f"Linked {linked_provider} identity {linked_value} "
                    f"to account {account.id}"
                ),
                actor_label="account-admin",
                api_key_name=request_api_key.name,
                api_key_fingerprint=request_api_key.fingerprint,
            )
        )
        session.add(
            AccountIdentity(
                account_id=account.id,
                provider=linked_provider,
                identity_value=linked_value,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await find_account(session, linked_provider, linked_value)
            if existing is not None and existing.id != account.id:
                try:
                    account = await merge_accounts(
                        session,
                        source_id=existing.id,
                        destination_id=account.id,
                        api_key_name=request_api_key.name,
                        api_key_fingerprint=request_api_key.fingerprint,
                        required_source_identity=(linked_provider, linked_value),
                        required_destination_identity=(provider, identity_value(identity, provider)),
                    )
                except IntegrityError:
                    await session.rollback()
                    raise HTTPException(status_code=409, detail="ACCOUNT_MERGE_CONFLICT")
            elif existing is None:
                raise HTTPException(status_code=409, detail="Identity link conflict")
    return await to_account_response(session, account)


@router.post(
    "/api/v1/accounts/{provider}/{identity}/migrate",
    response_model=AccountResponse,
    dependencies=[Depends(require_admin_api_key)],
)
async def migrate_identity(
    provider: IdentityProvider,
    identity: str,
    request: MigrateIdentityRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_admin_api_key),
) -> AccountResponse:
    old_value = identity_value(identity, provider)
    new_identity = request.identity
    if new_identity.provider != provider:
        raise HTTPException(
            status_code=422,
            detail="Migration must use the same identity provider",
        )
    new_value = identity_value(new_identity.value, new_identity.provider)
    source = await require_account(session, provider, old_value)
    if source.account_type != "user":
        raise HTTPException(status_code=409, detail="Only user identities can be migrated")
    if old_value == new_value:
        return await to_account_response(session, source)

    migration_key = f"identity-migration:{provider}:{old_value}:{new_value}"
    prior_migration = await session.scalar(
        select(Transaction).where(Transaction.idempotency_key == migration_key)
    )
    if prior_migration is not None:
        account = await find_account(session, provider, new_value)
        if account is not None:
            return await to_account_response(session, account)
        raise HTTPException(status_code=409, detail="Identity migration state is inconsistent")

    source_id = source.id
    destination = await find_account(session, provider, new_value)
    if destination is not None and destination.id != source_id:
        try:
            account = await merge_accounts(
                session,
                source_id=source_id,
                destination_id=destination.id,
                api_key_name=request_api_key.name,
                api_key_fingerprint=request_api_key.fingerprint,
                discard_identity=(provider, old_value),
                required_source_identity=(provider, old_value),
                required_destination_identity=(provider, new_value),
                audit_idempotency_key=migration_key,
            )
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="ACCOUNT_MERGE_CONFLICT")
        return await to_account_response(session, account)

    identity_row = await session.scalar(
        select(AccountIdentity)
        .where(
            AccountIdentity.account_id == source_id,
            AccountIdentity.provider == provider,
            AccountIdentity.identity_value == old_value,
        )
        .with_for_update()
    )
    if identity_row is None:
        raise HTTPException(status_code=409, detail="Identity migration source changed")
    identity_row.identity_value = new_value
    session.add(
        Transaction(
            idempotency_key=migration_key,
            transaction_type="identity_migration",
            reference_id=migration_key,
            description=f"Migrated {provider} identity {old_value} to {new_value}",
            actor_label="account-admin",
            api_key_name=request_api_key.name,
            api_key_fingerprint=request_api_key.fingerprint,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        destination = await find_account(session, provider, new_value)
        if destination is None or destination.id == source_id:
            raise HTTPException(status_code=409, detail="Identity migration conflict")
        try:
            account = await merge_accounts(
                session,
                source_id=source_id,
                destination_id=destination.id,
                api_key_name=request_api_key.name,
                api_key_fingerprint=request_api_key.fingerprint,
                discard_identity=(provider, old_value),
                required_source_identity=(provider, old_value),
                required_destination_identity=(provider, new_value),
                audit_idempotency_key=migration_key,
            )
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="ACCOUNT_MERGE_CONFLICT")
        return await to_account_response(session, account)

    await session.refresh(source)
    return await to_account_response(session, source)


async def adjust_balance(
    provider: IdentityProvider,
    identity: str,
    field: Literal["money", "credits"],
    request: AdjustmentRequest,
    session: AsyncSession,
    request_api_key: AuthenticatedApiKey,
) -> AccountResponse:
    identity = identity_value(identity, provider)
    account = await require_account(session, provider, identity)
    system_key = "external_money" if field == "money" else "external_credits"
    system_account = await require_system_account(session, system_key)

    # Lock both sides before checking idempotency or changing either balance.
    locked_accounts = await lock_accounts(session, {account.id, system_account.id})
    account = locked_accounts[account.id]
    existing = await session.scalar(
        select(Transaction).where(Transaction.idempotency_key == request.reference_id)
    )
    expected_description = f"{field}:{account.id}:{request.amount}:{request.reason}"
    if existing is not None:
        if existing.description != expected_description:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        return await to_account_response(session, account)
    if request.amount == 0:
        raise HTTPException(status_code=422, detail="Adjustment amount must not be zero")
    current = getattr(account, field)
    if current + request.amount < 0:
        raise HTTPException(status_code=409, detail="INSUFFICIENT_FUNDS")
    actor_fields = await transaction_actor_fields(session, request.actor)
    transaction = Transaction(
        idempotency_key=request.reference_id,
        transaction_type="admin_adjustment",
        reference_id=request.reference_id,
        description=expected_description,
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
        **actor_fields,
    )
    setattr(account, field, current + request.amount)
    transaction.entries.append(
        TransactionEntry(
            account=account,
            money_delta=request.amount if field == "money" else 0,
            credits_delta=request.amount if field == "credits" else 0,
        )
    )
    await add_system_entry(
        session,
        transaction,
        "external_money" if field == "money" else "external_credits",
        money_delta=-request.amount if field == "money" else 0,
        credits_delta=-request.amount if field == "credits" else 0,
    )
    session.add(transaction)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    await session.refresh(account)
    return await to_account_response(session, account)


@router.post(
    "/api/v1/accounts/{provider}/{identity}/money/adjust",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def adjust_money(
    provider: IdentityProvider,
    identity: str,
    request: AdjustmentRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> AccountResponse:
    return await adjust_balance(provider, identity, "money", request, session, request_api_key)


@router.post(
    "/api/v1/accounts/{provider}/{identity}/credits/adjust",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def adjust_credits(
    provider: IdentityProvider,
    identity: str,
    request: AdjustmentRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> AccountResponse:
    return await adjust_balance(provider, identity, "credits", request, session, request_api_key)


async def exchange(
    provider: IdentityProvider,
    identity: str,
    request: ExchangeRequest,
    purchase: bool,
    session: AsyncSession,
    request_api_key: AuthenticatedApiKey,
) -> AccountResponse:
    identity = identity_value(identity, provider)
    account = await require_account(session, provider, identity)
    external_money = await require_system_account(session, "external_money")
    external_credits = await require_system_account(session, "external_credits")

    # Acquire locks in a stable order to avoid deadlocks between concurrent exchanges.
    locked_accounts = await lock_accounts(
        session,
        {account.id, external_money.id, external_credits.id},
    )
    account = locked_accounts[account.id]
    price = get_settings().token_price
    operation = "purchase" if purchase else "sale"
    expected_description = f"{operation}:{account.id}:{request.amount}:{price}"
    existing = await session.scalar(
        select(Transaction).where(Transaction.idempotency_key == request.reference_id)
    )
    if existing is not None:
        if existing.description != expected_description:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        return await to_account_response(session, account)
    if (
        request.expected_price_per_credit is not None
        and request.expected_price_per_credit != price
    ):
        raise HTTPException(status_code=409, detail="TOKEN_PRICE_CHANGED")
    money_delta = -(request.amount * price) if purchase else request.amount * price
    credits_delta = request.amount if purchase else -request.amount
    if account.money + money_delta < 0 or account.credits + credits_delta < 0:
        raise HTTPException(status_code=409, detail="INSUFFICIENT_FUNDS")
    actor_fields = await transaction_actor_fields(session, request.actor)
    account.money += money_delta
    account.credits += credits_delta
    transaction = Transaction(
        idempotency_key=request.reference_id,
        transaction_type="credit_purchase" if purchase else "credit_sale",
        reference_id=request.reference_id,
        description=expected_description,
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
        **actor_fields,
    )
    transaction.entries.append(
        TransactionEntry(account=account, money_delta=money_delta, credits_delta=credits_delta)
    )
    await add_system_entry(
        session,
        transaction,
        "external_money",
        money_delta=-money_delta,
    )
    await add_system_entry(
        session,
        transaction,
        "external_credits",
        credits_delta=-credits_delta,
    )
    session.add(transaction)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    await session.refresh(account)
    return await to_account_response(session, account)


@router.post(
    "/api/v1/accounts/{provider}/{identity}/credits/purchase",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def purchase_credits(
    provider: IdentityProvider,
    identity: str,
    request: ExchangeRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> AccountResponse:
    return await exchange(provider, identity, request, True, session, request_api_key)


@router.post(
    "/api/v1/accounts/{provider}/{identity}/credits/sell",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def sell_credits(
    provider: IdentityProvider,
    identity: str,
    request: ExchangeRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> AccountResponse:
    return await exchange(provider, identity, request, False, session, request_api_key)


@router.get(
    "/api/v1/leaderboard",
    response_model=LeaderboardResponse,
    dependencies=[Depends(require_read_api_key)],
)
async def leaderboard(
    currency: Literal["money", "credits"] = Query("money"),
    limit: int = Query(10, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> LeaderboardResponse:
    column = Account.money if currency == "money" else Account.credits
    conditions = [Account.account_type.not_in(("casino", "system"))]
    accounts = (
        await session.scalars(
            select(Account)
            .where(*conditions)
            .order_by(column.desc())
            .limit(limit)
        )
    ).all()
    identities = await account_identities(session, [account.id for account in accounts])
    return LeaderboardResponse(
        currency=currency,
        entries=[
            LeaderboardEntry(
                account_id=account.id,
                steam_id=identities.get(account.id, {}).get("steam"),
                discord_id=identities.get(account.id, {}).get("discord"),
                balance=getattr(account, currency),
            )
            for account in accounts
        ],
    )
