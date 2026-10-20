"""Casino account binding and game lifecycle endpoints."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthenticatedApiKey, require_read_api_key, require_write_api_key
from ..config import get_settings
from ..db import get_session
from ..models import Account, CasinoGameSettlement, Transaction, TransactionEntry
from ..schemas import (
    AccountResponse,
    CasinoBindRequest,
    GameResponse,
    ReserveRequest,
    SettleRequest,
)
from ..services import (
    ensure_account,
    find_account,
    identity_value,
    lock_accounts,
    require_account,
    transaction_actor_fields,
    to_account_response,
)

router = APIRouter()


async def casino_account(session: AsyncSession) -> Account:
    account = await session.scalar(select(Account).where(Account.account_type == "casino"))
    if account is None:
        raise HTTPException(status_code=503, detail="Casino account is not bound")
    return account


@router.get(
    "/api/v1/casino/balance",
    response_model=AccountResponse,
    dependencies=[Depends(require_read_api_key)],
)
async def casino_balance(session: AsyncSession = Depends(get_session)) -> AccountResponse:
    account = await casino_account(session)
    await session.commit()
    await session.refresh(account)
    return await to_account_response(session, account)


@router.post(
    "/api/v1/casino/bind",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def bind_casino(
    request: CasinoBindRequest,
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    identity = request.identity
    value = identity_value(identity.value, identity.provider)
    casino = await session.scalar(
        select(Account).where(Account.account_type == "casino").with_for_update()
    )
    account = await find_account(session, identity.provider, value)
    if casino is not None and (account is None or casino.id != account.id):
        raise HTTPException(status_code=409, detail="CASINO_ALREADY_BOUND")
    if account is None:
        account = await ensure_account(session, identity.provider, value, account_type="casino")
    account.account_type = "casino"
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="CASINO_ALREADY_BOUND")
    await session.refresh(account)
    return await to_account_response(session, account)


def to_game_response(game: CasinoGameSettlement) -> GameResponse:
    return GameResponse(
        game_id=game.game_id,
        game_type=game.game_type,
        status=game.status,
        wager_currency=game.wager_currency,
        wager_amount=game.wager_amount,
        created_by_account_id=game.created_by_account_id,
        created_at=game.created_at,
        settled_at=game.settled_at,
        result=game.result,
        transaction_id=game.transaction_id,
    )


async def reservation_matches(
    session: AsyncSession,
    game: CasinoGameSettlement,
    *,
    game_type: str,
    currency: Literal["money", "credits"],
    player_amounts: dict[int, int],
    creator_id: int,
    casino_id: int,
    total: int,
    reference_id: str,
) -> bool:
    if (
        game.game_type != game_type
        or game.wager_currency != currency
        or game.wager_amount != total
        or game.created_by_account_id != creator_id
    ):
        return False
    transaction = await session.scalar(
        select(Transaction).where(Transaction.idempotency_key == reference_id)
    )
    if (
        transaction is None
        or transaction.transaction_type != f"{currency}_wager"
        or transaction.reference_id != reference_id
    ):
        return False
    entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == transaction.id)
        )
    ).all()

    # Compare grouped account deltas, independent of the database return order.
    expected = {
        account_id: (-amount, 0) if currency == "money" else (0, -amount)
        for account_id, amount in player_amounts.items()
    }
    expected[casino_id] = (total, 0) if currency == "money" else (0, total)
    actual = {
        entry.account_id: (entry.money_delta, entry.credits_delta)
        for entry in entries
    }
    return actual == expected


async def settlement_matches(
    session: AsyncSession,
    game: CasinoGameSettlement,
    *,
    winner_id: int,
    result: dict,
) -> bool:
    if game.status != "settled" or game.result is None or game.transaction_id is None:
        return False
    persisted_result = game.result
    if any(persisted_result.get(key) != value for key, value in result.items()):
        return False
    if set(persisted_result) != set(result) | {"fee", "payout"}:
        return False
    transaction = await session.get(Transaction, game.transaction_id)
    if (
        transaction is None
        or transaction.transaction_type != f"{game.wager_currency}_payout"
        or transaction.reference_id != str(game.game_id)
    ):
        return False
    payout = persisted_result["payout"]
    winner_delta = (payout, 0) if game.wager_currency == "money" else (0, payout)
    casino_delta = (-payout, 0) if game.wager_currency == "money" else (0, -payout)
    entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == transaction.id)
        )
    ).all()
    return {
        entry.account_id: (entry.money_delta, entry.credits_delta)
        for entry in entries
    } == {
        winner_id: winner_delta,
        (await casino_account(session)).id: casino_delta,
    }


@router.get(
    "/api/v1/games/{game_id}",
    response_model=GameResponse,
    dependencies=[Depends(require_read_api_key)],
)
async def get_game(game_id: UUID, session: AsyncSession = Depends(get_session)) -> GameResponse:
    game = await session.get(CasinoGameSettlement, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    return to_game_response(game)


@router.post(
    "/api/v1/games/{game_id}/reserve",
    response_model=GameResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def reserve_game(
    game_id: UUID,
    request: ReserveRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> GameResponse:
    casino = await casino_account(session)
    amounts: dict[int, int] = {}
    player_ids: list[int] = []
    for player in request.players:
        player_account = await require_account(
            session,
            player.identity.provider,
            identity_value(player.identity.value, player.identity.provider),
        )
        amounts[player_account.id] = amounts.get(player_account.id, 0) + player.amount
        player_ids.append(player_account.id)
    if casino.id in amounts:
        raise HTTPException(status_code=422, detail="Casino cannot wager against itself")
    casino_id = casino.id
    total = sum(amounts.values())
    # The first submitted player remains the creator when multiple identities group by account.
    creator_id = player_ids[0]
    ids = sorted([*amounts, casino.id])
    accounts = (
        await session.scalars(
            select(Account).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()
        )
    ).all()
    by_id = {account.id: account for account in accounts}
    existing = await session.get(CasinoGameSettlement, game_id)
    if existing is not None:
        if await reservation_matches(
            session,
            existing,
            game_type=request.game_type,
            currency=request.currency,
            player_amounts=amounts,
            creator_id=creator_id,
            casino_id=casino_id,
            total=total,
            reference_id=request.reference_id,
        ):
            return to_game_response(existing)
        raise HTTPException(status_code=409, detail="GAME_RETRY_CONFLICT")
    column = "money" if request.currency == "money" else "credits"
    if any(getattr(by_id[account_id], column) < amount for account_id, amount in amounts.items()):
        raise HTTPException(status_code=409, detail="INSUFFICIENT_FUNDS")
    if getattr(by_id[casino_id], column) > 9_223_372_036_854_775_807 - total:
        raise HTTPException(status_code=409, detail="CASINO_BALANCE_LIMIT")
    for account_id, amount in amounts.items():
        setattr(by_id[account_id], column, getattr(by_id[account_id], column) - amount)
    setattr(by_id[casino.id], column, getattr(by_id[casino.id], column) + total)
    actor_fields = await transaction_actor_fields(session, request.actor)
    transaction = Transaction(
        idempotency_key=request.reference_id,
        transaction_type=f"{request.currency}_wager",
        reference_id=request.reference_id,
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
        **actor_fields,
    )
    for account_id, amount in amounts.items():
        transaction.entries.append(
            TransactionEntry(
                account=by_id[account_id],
                money_delta=-amount if request.currency == "money" else 0,
                credits_delta=-amount if request.currency == "credits" else 0,
            )
        )
    transaction.entries.append(
        TransactionEntry(
            account=by_id[casino.id],
            money_delta=total if request.currency == "money" else 0,
            credits_delta=total if request.currency == "credits" else 0,
        )
    )
    session.add(transaction)
    await session.flush()
    game = CasinoGameSettlement(
        game_id=game_id,
        game_type=request.game_type,
        status="reserved",
        wager_currency=request.currency,
        wager_amount=total,
        created_by_account_id=creator_id,
        transaction_id=transaction.id,
    )
    session.add(game)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(CasinoGameSettlement, game_id)
        if existing is not None and await reservation_matches(
            session,
            existing,
            game_type=request.game_type,
            currency=request.currency,
            player_amounts=amounts,
            creator_id=creator_id,
            casino_id=casino_id,
            total=total,
            reference_id=request.reference_id,
        ):
            return to_game_response(existing)
        raise HTTPException(status_code=409, detail="GAME_RETRY_CONFLICT")
    await session.refresh(game)
    return to_game_response(game)


@router.post(
    "/api/v1/games/{game_id}/settle",
    response_model=GameResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def settle_game(
    game_id: UUID,
    request: SettleRequest,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> GameResponse:
    game = await session.scalar(
        select(CasinoGameSettlement)
        .where(CasinoGameSettlement.game_id == game_id)
        .with_for_update()
    )
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    winner = await require_account(
        session,
        request.winner.provider,
        identity_value(request.winner.value, request.winner.provider),
    )
    if game.status == "settled":
        if await settlement_matches(
            session,
            game,
            winner_id=winner.id,
            result=request.result,
        ):
            return to_game_response(game)
        raise HTTPException(status_code=409, detail="GAME_SETTLEMENT_CONFLICT")
    if game.status != "reserved":
        raise HTTPException(status_code=409, detail="GAME_NOT_SETTLEABLE")
    reserve_entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == game.transaction_id)
        )
    ).all()
    player_entries = [
        entry
        for entry in reserve_entries
        if entry.money_delta < 0 or entry.credits_delta < 0
    ]
    if not any(entry.account_id == winner.id for entry in player_entries):
        raise HTTPException(status_code=422, detail="Winner did not participate in game")
    casino = await casino_account(session)
    settlement_accounts_by_id = await lock_accounts(session, {winner.id, casino.id})
    winner = settlement_accounts_by_id[winner.id]
    casino = settlement_accounts_by_id[casino.id]
    fee = 0
    if get_settings().challenge_fee_enabled:
        fee = game.wager_amount * get_settings().challenge_fee_percent // 100
    payout = game.wager_amount - fee
    money_delta = payout if game.wager_currency == "money" else 0
    credits_delta = payout if game.wager_currency == "credits" else 0
    casino_balance = casino.money if game.wager_currency == "money" else casino.credits
    winner_balance = winner.money if game.wager_currency == "money" else winner.credits
    if casino_balance < payout:
        raise HTTPException(status_code=409, detail="CASINO_INSUFFICIENT_FUNDS")
    if winner_balance > 9_223_372_036_854_775_807 - payout:
        raise HTTPException(status_code=409, detail="WINNER_BALANCE_LIMIT")
    if game.wager_currency == "money":
        winner.money += payout
        casino.money -= payout
    else:
        winner.credits += payout
        casino.credits -= payout
    actor_fields = await transaction_actor_fields(session, request.actor)
    transaction = Transaction(
        idempotency_key=f"game-settle:{game_id}",
        transaction_type=f"{game.wager_currency}_payout",
        reference_id=str(game_id),
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
        **actor_fields,
    )
    transaction.entries.append(
        TransactionEntry(account=winner, money_delta=money_delta, credits_delta=credits_delta)
    )
    transaction.entries.append(
        TransactionEntry(account=casino, money_delta=-money_delta, credits_delta=-credits_delta)
    )
    game.status = "settled"
    game.settled_at = datetime.now(timezone.utc)
    game.result = {**request.result, "fee": fee, "payout": payout}
    game.transaction_id = transaction.id
    session.add(transaction)
    await session.commit()
    await session.refresh(game)
    return to_game_response(game)


@router.post(
    "/api/v1/games/{game_id}/refund",
    response_model=GameResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def refund_game(
    game_id: UUID,
    session: AsyncSession = Depends(get_session),
    request_api_key: AuthenticatedApiKey = Depends(require_write_api_key),
) -> GameResponse:
    game = await session.scalar(
        select(CasinoGameSettlement)
        .where(CasinoGameSettlement.game_id == game_id)
        .with_for_update()
    )
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if game.status == "refunded":
        return to_game_response(game)
    if game.status != "reserved":
        raise HTTPException(status_code=409, detail="GAME_NOT_REFUNDABLE")
    reservation_transaction_id = game.transaction_id
    reserve_entries = (
        await session.scalars(
            select(TransactionEntry).where(
                TransactionEntry.transaction_id == reservation_transaction_id
            )
        )
    ).all()
    account_ids = sorted({entry.account_id for entry in reserve_entries})
    accounts = (
        await session.scalars(
            select(Account)
            .where(Account.id.in_(account_ids))
            .order_by(Account.id)
            .with_for_update()
        )
    ).all()
    by_id = {account.id: account for account in accounts}
    for entry in reserve_entries:
        account = by_id[entry.account_id]
        next_money = account.money - entry.money_delta
        next_credits = account.credits - entry.credits_delta
        if account.account_type != "system" and (next_money < 0 or next_credits < 0):
            raise HTTPException(status_code=409, detail="REFUND_INSUFFICIENT_FUNDS")
        if account.account_type != "system" and (
            next_money > 9_223_372_036_854_775_807
            or next_credits > 9_223_372_036_854_775_807
        ):
            raise HTTPException(status_code=409, detail="REFUND_BALANCE_LIMIT")
    transaction = Transaction(
        idempotency_key=f"game-refund:{game_id}",
        transaction_type=f"{game.wager_currency}_refund",
        reference_id=str(game_id),
        reversal_of_transaction_id=reservation_transaction_id,
        api_key_name=request_api_key.name,
        api_key_fingerprint=request_api_key.fingerprint,
        actor_label="game_refund",
    )
    for entry in reserve_entries:
        account = by_id[entry.account_id]
        money_delta = -entry.money_delta
        credits_delta = -entry.credits_delta
        account.money += money_delta
        account.credits += credits_delta
        transaction.entries.append(
            TransactionEntry(
                account=account,
                money_delta=money_delta,
                credits_delta=credits_delta,
            )
        )
    game.status = "refunded"
    game.settled_at = datetime.now(timezone.utc)
    game.transaction_id = transaction.id
    session.add(transaction)
    await session.commit()
    await session.refresh(game)
    return to_game_response(game)
