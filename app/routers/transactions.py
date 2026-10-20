"""Transfer, transaction-history, and reversal endpoints."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthenticatedApiKey, require_read_api_key, require_write_api_key
from ..db import get_session
from ..models import Account, Transaction, TransactionEntry
from ..schemas import ReverseTransactionRequest, TransactionResponse, TransferRequest
from ..services import (
    IdentityProvider,
    identity_value,
    lock_accounts,
    require_account,
    transaction_actor_fields,
    transaction_response,
    transaction_responses,
)

router = APIRouter()


async def transfer_matches(
    session: AsyncSession,
    transaction: Transaction,
    *,
    sender_id: int,
    recipient_id: int,
    currency: Literal["money", "credits"],
    amount: int,
    reference_id: str,
) -> bool:
    if (
        transaction.transaction_type != f"{currency}_transfer"
        or transaction.reference_id != reference_id
        or transaction.actor_account_id != sender_id
    ):
        return False

    # A retry is safe only when the persisted ledger entries are an exact match.
    entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == transaction.id)
        )
    ).all()
    expected = {
        sender_id: (-amount, 0) if currency == "money" else (0, -amount),
        recipient_id: (amount, 0) if currency == "money" else (0, amount),
    }
    actual = {
        entry.account_id: (entry.money_delta, entry.credits_delta)
        for entry in entries
    }
    return actual == expected


@router.post(
    "/api/v1/transfers",
    response_model=TransactionResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def create_transfer(
    request: TransferRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> TransactionResponse:
    sender = await require_account(
        session,
        request.sender.provider,
        request.sender.value,
    )
    recipient = await require_account(
        session,
        request.recipient.provider,
        request.recipient.value,
    )
    if sender.id == recipient.id:
        raise HTTPException(
            status_code=422,
            detail="Sender and recipient must be different accounts",
        )

    sender_id = sender.id
    recipient_id = recipient.id

    # Lock account rows in a deterministic order before reading or changing balances.
    accounts = await lock_accounts(session, {sender.id, recipient.id})
    sender = accounts[sender.id]
    recipient = accounts[recipient.id]
    idempotency_key = request.reference_id
    existing = await session.scalar(
        select(Transaction).where(Transaction.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if await transfer_matches(
            session,
            existing,
            sender_id=sender_id,
            recipient_id=recipient_id,
            currency=request.currency,
            amount=request.amount,
            reference_id=request.reference_id,
        ):
            return await transaction_response(session, existing)
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")

    balance = getattr(sender, request.currency)
    recipient_balance = getattr(recipient, request.currency)
    if balance < request.amount:
        raise HTTPException(status_code=409, detail="INSUFFICIENT_FUNDS")
    if recipient_balance > 9_223_372_036_854_775_807 - request.amount:
        raise HTTPException(status_code=409, detail="RECIPIENT_BALANCE_LIMIT")

    transaction = Transaction(
        idempotency_key=idempotency_key,
        transaction_type=f"{request.currency}_transfer",
        reference_id=request.reference_id,
        description=f"{request.currency} transfer",
        actor_account_id=sender_id,
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
    )
    setattr(sender, request.currency, balance - request.amount)
    setattr(recipient, request.currency, recipient_balance + request.amount)
    transaction.entries.extend(
        [
            TransactionEntry(
                account=sender,
                money_delta=-request.amount if request.currency == "money" else 0,
                credits_delta=-request.amount if request.currency == "credits" else 0,
            ),
            TransactionEntry(
                account=recipient,
                money_delta=request.amount if request.currency == "money" else 0,
                credits_delta=request.amount if request.currency == "credits" else 0,
            ),
        ]
    )
    session.add(transaction)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Transaction).where(Transaction.idempotency_key == idempotency_key)
        )
        if existing is not None and await transfer_matches(
            session,
            existing,
            sender_id=sender_id,
            recipient_id=recipient_id,
            currency=request.currency,
            amount=request.amount,
            reference_id=request.reference_id,
        ):
            return await transaction_response(session, existing)
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    await session.refresh(transaction)
    return await transaction_response(session, transaction)


@router.get(
    "/api/v1/accounts/{provider}/{identity}/transactions",
    response_model=list[TransactionResponse],
    dependencies=[Depends(require_read_api_key)],
)
async def account_transactions(
    provider: IdentityProvider,
    identity: str,
    limit: int = Query(50, ge=1, le=100),
    before: UUID | None = Query(
        None,
        description="Return transactions older than this transaction",
    ),
    session: AsyncSession = Depends(get_session),
) -> list[TransactionResponse]:
    account = await require_account(session, provider, identity_value(identity, provider))
    statement = (
        select(Transaction)
        .join(TransactionEntry)
        .where(TransactionEntry.account_id == account.id)
    )
    if before is not None:
        cursor = await session.scalar(
            select(Transaction)
            .join(TransactionEntry)
            .where(Transaction.id == before, TransactionEntry.account_id == account.id)
        )
        if cursor is None:
            raise HTTPException(
                status_code=404,
                detail="Transaction cursor not found for this account",
            )

        # UUID breaks ties when multiple transactions share the same timestamp.
        statement = statement.where(
            or_(
                Transaction.created_at < cursor.created_at,
                and_(
                    Transaction.created_at == cursor.created_at,
                    Transaction.id < cursor.id,
                ),
            )
        )
    transactions = (
        await session.scalars(
            statement
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .limit(limit)
        )
    ).unique().all()
    return await transaction_responses(session, list(transactions))


@router.post(
    "/api/v1/transactions/{transaction_id}/reverse",
    response_model=TransactionResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def reverse_transaction(
    transaction_id: UUID,
    request: ReverseTransactionRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> TransactionResponse:
    original = await session.scalar(
        select(Transaction).where(Transaction.id == transaction_id).with_for_update()
    )
    if original is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if original.reversal_of_transaction_id is not None:
        raise HTTPException(
            status_code=409,
            detail="A reversal transaction cannot itself be reversed",
        )
    if original.transaction_type.endswith(("_wager", "_payout", "_refund")):
        raise HTTPException(
            status_code=409,
            detail="Game transactions must be corrected through the game workflow",
        )
    prior_reversal = await session.scalar(
        select(Transaction).where(Transaction.reversal_of_transaction_id == original.id)
    )
    if prior_reversal is not None:
        if (
            prior_reversal.idempotency_key == request.reference_id
            and prior_reversal.description == request.reason
        ):
            return await transaction_response(session, prior_reversal)
        raise HTTPException(status_code=409, detail="Transaction has already been reversed")
    entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == original.id)
        )
    ).all()
    if not entries or sum(entry.money_delta for entry in entries) or sum(
        entry.credits_delta for entry in entries
    ):
        raise HTTPException(
            status_code=409,
            detail="Transaction cannot be reversed because its ledger entries are not balanced",
        )
    deltas: dict[int, list[int]] = {}
    # Aggregate first so repeated entries for an account produce one balance update.
    for entry in entries:
        totals = deltas.setdefault(entry.account_id, [0, 0])
        totals[0] -= entry.money_delta
        totals[1] -= entry.credits_delta
    accounts = (
        await session.scalars(
            select(Account)
            .where(Account.id.in_(sorted(deltas)))
            .order_by(Account.id)
            .with_for_update()
        )
    ).all()
    by_id = {account.id: account for account in accounts}
    for account_id, (money_delta, credits_delta) in deltas.items():
        account = by_id[account_id]
        if account.account_type != "system" and (
            account.money + money_delta < 0 or account.credits + credits_delta < 0
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Reversal would make account {account_id} balance negative",
            )
    actor_fields = await transaction_actor_fields(session, request.actor)
    reversal = Transaction(
        idempotency_key=request.reference_id,
        transaction_type="reversal",
        reference_id=request.reference_id,
        description=request.reason,
        reversal_of_transaction_id=original.id,
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
        **actor_fields,
    )
    for account_id, (money_delta, credits_delta) in deltas.items():
        account = by_id[account_id]
        account.money += money_delta
        account.credits += credits_delta
        reversal.entries.append(
            TransactionEntry(
                account=account,
                money_delta=money_delta,
                credits_delta=credits_delta,
            )
        )
    session.add(reversal)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Transaction reversal conflict")
    await session.refresh(reversal)
    return await transaction_response(session, reversal)
