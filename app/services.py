"""Shared account, identity, and ledger operations for API routers."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .identities import validate_identity_value
from .models import (
    Account,
    AccountIdentity,
    CasinoGameSettlement,
    Transaction,
    TransactionEntry,
)
from .schemas import (
    AccountResponse,
    TransactionActor,
    TransactionEntryResponse,
    TransactionResponse,
)

IdentityProvider = Literal["steam", "discord"]


def identity_value(identity: str, provider: str) -> str:
    try:
        return validate_identity_value(provider, identity)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid provider or identity value")


async def find_account(
    session: AsyncSession,
    provider: IdentityProvider,
    value: str,
    *,
    lock: bool = False,
) -> Account | None:
    statement = (
        select(Account)
        .join(AccountIdentity)
        .where(AccountIdentity.provider == provider, AccountIdentity.identity_value == value)
    )
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def require_account(
    session: AsyncSession,
    provider: IdentityProvider,
    value: str,
    *,
    lock: bool = False,
) -> Account:
    account = await find_account(session, provider, value, lock=lock)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


async def ensure_account(
    session: AsyncSession,
    provider: IdentityProvider,
    value: str,
    *,
    account_type: str = "user",
) -> Account:
    account = await find_account(session, provider, value)
    if account is not None:
        return account
    account = Account(account_type=account_type)
    account.identities.append(AccountIdentity(provider=provider, identity_value=value))
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        # Another request may have created this identity after the initial lookup.
        await session.rollback()
        account = await find_account(session, provider, value)
        if account is None:
            raise
        return account
    await session.refresh(account)
    return account


async def resolve_actor(
    session: AsyncSession,
    actor: TransactionActor,
) -> tuple[int | None, str | None]:
    if actor.identity is not None:
        account = await require_account(
            session,
            actor.identity.provider,
            identity_value(actor.identity.value, actor.identity.provider),
        )
        return account.id, None
    return None, actor.label


async def account_identities(
    session: AsyncSession,
    account_ids: list[int],
) -> dict[int, dict[str, str]]:
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AccountIdentity.account_id,
                AccountIdentity.provider,
                AccountIdentity.identity_value,
            )
            .where(AccountIdentity.account_id.in_(account_ids))
        )
    ).all()
    result: dict[int, dict[str, str]] = {}
    for account_id, provider, value in rows:
        result.setdefault(account_id, {})[provider] = value
    return result


async def lock_accounts(session: AsyncSession, account_ids: set[int]) -> dict[int, Account]:
    """Lock accounts in primary-key order to reduce concurrent deadlocks."""
    accounts = (
        await session.scalars(
            select(Account)
            .where(Account.id.in_(sorted(account_ids)))
            .order_by(Account.id)
            .with_for_update()
        )
    ).all()
    return {account.id: account for account in accounts}


async def merge_accounts(
    session: AsyncSession,
    *,
    source_id: int,
    destination_id: int,
    api_key_name: str,
    api_key_fingerprint: str,
    discard_identity: tuple[IdentityProvider, str] | None = None,
    required_source_identity: tuple[IdentityProvider, str] | None = None,
    required_destination_identity: tuple[IdentityProvider, str] | None = None,
    audit_idempotency_key: str | None = None,
) -> Account:
    """Merge source into destination, preserving balances and historical references."""
    accounts = await lock_accounts(session, {source_id, destination_id})
    source = accounts.get(source_id)
    destination = accounts.get(destination_id)
    if source is None or destination is None:
        raise HTTPException(status_code=409, detail="ACCOUNT_MERGE_CONFLICT")
    if source.account_type != "user" or destination.account_type != "user":
        raise HTTPException(status_code=409, detail="Only user accounts can be merged")

    identities = (
        await session.scalars(
            select(AccountIdentity)
            .where(AccountIdentity.account_id.in_((source_id, destination_id)))
            .order_by(AccountIdentity.id)
            .with_for_update()
        )
    ).all()
    source_identities = [identity for identity in identities if identity.account_id == source_id]
    destination_providers = {
        identity.provider: identity.identity_value
        for identity in identities
        if identity.account_id == destination_id
    }
    source_providers = {
        identity.provider: identity.identity_value
        for identity in source_identities
    }
    if (
        required_source_identity is not None
        and source_providers.get(required_source_identity[0]) != required_source_identity[1]
    ):
        raise HTTPException(status_code=409, detail="Account merge source identity changed")
    if (
        required_destination_identity is not None
        and destination_providers.get(required_destination_identity[0])
        != required_destination_identity[1]
    ):
        raise HTTPException(status_code=409, detail="Account merge destination identity changed")
    discarded = False
    for identity in source_identities:
        if discard_identity == (identity.provider, identity.identity_value):
            discarded = True
            continue
        destination_value = destination_providers.get(identity.provider)
        if destination_value is not None and destination_value != identity.identity_value:
            raise HTTPException(
                status_code=409,
                detail=f"Account merge would conflict with the existing {identity.provider} identity",
            )
    if discard_identity is not None and not discarded:
        raise HTTPException(status_code=409, detail="Identity migration source changed")

    max_balance = 9_223_372_036_854_775_807
    merged_money = source.money + destination.money
    merged_credits = source.credits + destination.credits
    if merged_money > max_balance or merged_credits > max_balance:
        raise HTTPException(status_code=409, detail="ACCOUNT_BALANCE_LIMIT")

    source_identity_values = [
        f"{identity.provider}:{identity.identity_value}"
        for identity in source_identities
    ]
    destination.money = merged_money
    destination.credits = merged_credits

    # Drop only the replaced identity during recovery; move every other identity.
    if discard_identity is not None:
        await session.execute(
            delete(AccountIdentity).where(
                AccountIdentity.account_id == source_id,
                AccountIdentity.provider == discard_identity[0],
                AccountIdentity.identity_value == discard_identity[1],
            )
        )
    await session.execute(
        update(AccountIdentity)
        .where(AccountIdentity.account_id == source_id)
        .values(account_id=destination_id)
    )
    await session.execute(
        update(TransactionEntry)
        .where(TransactionEntry.account_id == source_id)
        .values(account_id=destination_id)
    )
    await session.execute(
        update(Transaction)
        .where(Transaction.actor_account_id == source_id)
        .values(actor_account_id=destination_id)
    )
    await session.execute(
        update(CasinoGameSettlement)
        .where(CasinoGameSettlement.created_by_account_id == source_id)
        .values(created_by_account_id=destination_id)
    )
    await session.delete(source)

    audit = Transaction(
        idempotency_key=audit_idempotency_key or f"account-merge:{uuid4()}",
        transaction_type="identity_migration" if audit_idempotency_key else "account_merge",
        reference_id=audit_idempotency_key or f"account-merge:{source_id}:{destination_id}",
        description=(
            f"Merged account {source_id} into {destination_id}; "
            f"source identities: {', '.join(source_identity_values)}"
        ),
        actor_label="account-admin",
        api_key_name=api_key_name,
        api_key_fingerprint=api_key_fingerprint,
    )
    session.add(audit)
    await session.commit()
    await session.refresh(destination)
    return destination


async def require_system_account(session: AsyncSession, system_key: str) -> Account:
    account = await session.scalar(select(Account).where(Account.system_key == system_key))
    if account is None:
        raise HTTPException(
            status_code=503,
            detail=f"System ledger account '{system_key}' is missing",
        )
    return account


async def to_account_response(session: AsyncSession, account: Account) -> AccountResponse:
    identities = (await account_identities(session, [account.id])).get(account.id, {})
    return AccountResponse(
        account_id=account.id,
        account_type=account.account_type,
        system_key=account.system_key,
        steam_id=identities.get("steam"),
        discord_id=identities.get("discord"),
        money=account.money,
        credits=account.credits,
        is_casino_account=account.account_type == "casino",
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


async def transaction_response(
    session: AsyncSession,
    transaction: Transaction,
) -> TransactionResponse:
    return (await transaction_responses(session, [transaction]))[0]


async def transaction_responses(
    session: AsyncSession,
    transactions: list[Transaction],
) -> list[TransactionResponse]:
    """Serialize a page using batched entry, account, identity, and reversal queries."""
    if not transactions:
        return []

    transaction_ids = [transaction.id for transaction in transactions]
    entries = (
        await session.scalars(
            select(TransactionEntry)
            .where(TransactionEntry.transaction_id.in_(transaction_ids))
            .order_by(TransactionEntry.transaction_id, TransactionEntry.account_id)
        )
    ).all()
    entries_by_transaction: dict[UUID, list[TransactionEntry]] = {}
    account_ids = {
        entry.account_id
        for entry in entries
    } | {
        transaction.actor_account_id
        for transaction in transactions
        if transaction.actor_account_id is not None
    }
    for entry in entries:
        entries_by_transaction.setdefault(entry.transaction_id, []).append(entry)

    # Fetch enrichment data in batches rather than once per transaction.
    accounts = (
        await session.scalars(select(Account).where(Account.id.in_(account_ids)))
    ).all()
    accounts_by_id = {account.id: account for account in accounts}
    identities = await account_identities(session, list(account_ids))
    reversed_rows = (
        await session.execute(
            select(Transaction.reversal_of_transaction_id, Transaction.id).where(
                Transaction.reversal_of_transaction_id.in_(transaction_ids)
            )
        )
    ).all()
    reversed_by = {original_id: reversal_id for original_id, reversal_id in reversed_rows}

    responses: list[TransactionResponse] = []
    for transaction in transactions:
        actor_identities = identities.get(transaction.actor_account_id, {})
        responses.append(
            TransactionResponse(
                id=transaction.id,
                transaction_type=transaction.transaction_type,
                reference_id=transaction.reference_id,
                description=transaction.description,
                actor_account_id=transaction.actor_account_id,
                actor_steam_id=actor_identities.get("steam"),
                actor_discord_id=actor_identities.get("discord"),
                actor_label=transaction.actor_label,
                api_key_name=transaction.api_key_name,
                api_key_fingerprint=transaction.api_key_fingerprint,
                reversal_of_transaction_id=transaction.reversal_of_transaction_id,
                reversed_by_transaction_id=reversed_by.get(transaction.id),
                created_at=transaction.created_at,
                entries=[
                    TransactionEntryResponse(
                        account_id=entry.account_id,
                        account_type=accounts_by_id[entry.account_id].account_type,
                        system_key=accounts_by_id[entry.account_id].system_key,
                        steam_id=identities.get(entry.account_id, {}).get("steam"),
                        discord_id=identities.get(entry.account_id, {}).get("discord"),
                        money_delta=entry.money_delta,
                        credits_delta=entry.credits_delta,
                    )
                    for entry in entries_by_transaction.get(transaction.id, [])
                ],
            )
        )
    return responses


async def transaction_actor_fields(
    session: AsyncSession,
    actor: TransactionActor,
) -> dict[str, int | str | None]:
    account_id, label = await resolve_actor(session, actor)
    return {"actor_account_id": account_id, "actor_label": label}


async def add_system_entry(
    session: AsyncSession,
    transaction: Transaction,
    system_key: str,
    *,
    money_delta: int = 0,
    credits_delta: int = 0,
) -> None:
    account = await require_system_account(session, system_key)
    account.money += money_delta
    account.credits += credits_delta
    transaction.entries.append(
        TransactionEntry(
            account=account,
            money_delta=money_delta,
            credits_delta=credits_delta,
        )
    )
