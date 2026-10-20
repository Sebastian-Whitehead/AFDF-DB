from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import AuthenticatedApiKey, require_read_api_key, require_write_api_key
from .config import get_settings
from .db import engine, get_session
from .models import Account, AccountIdentity, CasinoGameSettlement, Transaction, TransactionEntry
from .schemas import (
    AccountResponse,
    AdjustmentRequest,
    CasinoBindRequest,
    ExchangeRequest,
    GameResponse,
    LeaderboardEntry,
    LeaderboardResponse,
    LinkIdentityRequest,
    ReserveRequest,
    ReverseTransactionRequest,
    SettleRequest,
    TransactionActor,
    TransactionEntryResponse,
    TransactionResponse,
)

app = FastAPI(title="Economy Service", version="0.1.0")
IdentityProvider = Literal["steam", "discord"]


@app.on_event("shutdown")
async def shutdown() -> None:
    await engine.dispose()


def identity_value(identity: str, provider: str) -> str:
    if provider not in ("steam", "discord") or not identity.isdecimal() or len(identity) > 20:
        raise HTTPException(status_code=422, detail="Invalid provider or identity value")
    return identity


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
        await session.rollback()
        account = await find_account(session, provider, value)
        if account is None:
            raise
        return account
    await session.refresh(account)
    return account


async def resolve_actor(session: AsyncSession, actor: TransactionActor) -> tuple[int | None, str | None]:
    if actor.identity is not None:
        account = await require_account(
            session,
            actor.identity.provider,
            identity_value(actor.identity.value, actor.identity.provider),
        )
        return account.id, None
    return None, actor.label


async def account_identities(session: AsyncSession, account_ids: list[int]) -> dict[int, dict[str, str]]:
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            select(AccountIdentity.account_id, AccountIdentity.provider, AccountIdentity.identity_value)
            .where(AccountIdentity.account_id.in_(account_ids))
        )
    ).all()
    result: dict[int, dict[str, str]] = {}
    for account_id, provider, value in rows:
        result.setdefault(account_id, {})[provider] = value
    return result


async def lock_accounts(session: AsyncSession, account_ids: set[int]) -> dict[int, Account]:
    accounts = (
        await session.scalars(
            select(Account)
            .where(Account.id.in_(sorted(account_ids)))
            .order_by(Account.id)
            .with_for_update()
        )
    ).all()
    return {account.id: account for account in accounts}


async def require_system_account(session: AsyncSession, system_key: str) -> Account:
    account = await session.scalar(select(Account).where(Account.system_key == system_key))
    if account is None:
        raise HTTPException(status_code=503, detail=f"System ledger account '{system_key}' is missing")
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


async def transaction_response(session: AsyncSession, transaction: Transaction) -> TransactionResponse:
    entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == transaction.id)
        )
    ).all()
    account_ids = [entry.account_id for entry in entries]
    if transaction.actor_account_id is not None:
        account_ids.append(transaction.actor_account_id)
    identities = await account_identities(session, account_ids)
    accounts = (
        await session.scalars(select(Account).where(Account.id.in_(account_ids)))
    ).all()
    accounts_by_id = {account.id: account for account in accounts}
    actor_identities = identities.get(transaction.actor_account_id, {})
    reversed_by = await session.scalar(
        select(Transaction.id).where(Transaction.reversal_of_transaction_id == transaction.id)
    )
    return TransactionResponse(
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
        reversed_by_transaction_id=reversed_by,
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
            for entry in entries
        ],
    )


async def casino_account(session: AsyncSession) -> Account:
    account = await session.scalar(select(Account).where(Account.account_type == "casino"))
    if account is None:
        raise HTTPException(status_code=503, detail="Casino account is not bound")
    return account


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


@app.middleware("http")
async def request_id(request: Request, call_next):
    correlation_id = request.headers.get("X-Request-ID", str(uuid4()))
    response = await call_next(request)
    response.headers["X-Request-ID"] = correlation_id
    return response


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as error:
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": str(error)})
    return {"status": "ready"}


@app.get(
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


@app.put(
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


@app.post(
    "/api/v1/accounts/{provider}/{identity}/identities",
    response_model=AccountResponse,
    dependencies=[Depends(require_write_api_key)],
)
async def link_identity(
    provider: IdentityProvider,
    identity: str,
    request: LinkIdentityRequest,
    session: AsyncSession = Depends(get_session),
) -> AccountResponse:
    account = await require_account(session, provider, identity_value(identity, provider), lock=True)
    linked_provider = request.identity.provider
    linked_value = identity_value(request.identity.value, linked_provider)
    existing = await find_account(session, linked_provider, linked_value)
    if existing is not None and existing.id != account.id:
        raise HTTPException(status_code=409, detail="Identity is already linked to another account")
    current = await session.scalar(
        select(AccountIdentity).where(
            AccountIdentity.account_id == account.id,
            AccountIdentity.provider == linked_provider,
        )
    )
    if current is not None and current.identity_value != linked_value:
        raise HTTPException(status_code=409, detail="Account already has an identity for this provider")
    if current is None:
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
            raise HTTPException(status_code=409, detail="Identity link conflict")
    return await to_account_response(session, account)


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


@app.post(
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


@app.post(
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
    locked_accounts = await lock_accounts(
        session,
        {account.id, external_money.id, external_credits.id},
    )
    account = locked_accounts[account.id]
    price = get_settings().token_price
    expected_description = f"{'purchase' if purchase else 'sale'}:{account.id}:{request.amount}:{price}"
    existing = await session.scalar(
        select(Transaction).where(Transaction.idempotency_key == request.reference_id)
    )
    if existing is not None:
        if existing.description != expected_description:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        return await to_account_response(session, account)
    if request.expected_price_per_credit is not None and request.expected_price_per_credit != price:
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


@app.post(
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


@app.post(
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


@app.get(
    "/api/v1/accounts/{provider}/{identity}/transactions",
    response_model=list[TransactionResponse],
    dependencies=[Depends(require_read_api_key)],
)
async def account_transactions(
    provider: IdentityProvider,
    identity: str,
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[TransactionResponse]:
    account = await require_account(session, provider, identity_value(identity, provider))
    transactions = (
        await session.scalars(
            select(Transaction)
            .join(TransactionEntry)
            .where(TransactionEntry.account_id == account.id)
            .order_by(Transaction.created_at.desc())
            .limit(limit)
        )
    ).unique().all()
    return [await transaction_response(session, transaction) for transaction in transactions]


@app.get(
    "/api/v1/casino/balance",
    response_model=AccountResponse,
    dependencies=[Depends(require_read_api_key)],
)
async def casino_balance(session: AsyncSession = Depends(get_session)) -> AccountResponse:
    account = await casino_account(session)
    await session.commit()
    await session.refresh(account)
    return await to_account_response(session, account)


@app.post(
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


@app.get(
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


@app.post(
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
        raise HTTPException(status_code=409, detail="A reversal transaction cannot itself be reversed")
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


@app.get(
    "/api/v1/games/{game_id}",
    response_model=GameResponse,
    dependencies=[Depends(require_read_api_key)],
)
async def get_game(game_id: UUID, session: AsyncSession = Depends(get_session)) -> GameResponse:
    game = await session.get(CasinoGameSettlement, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    return to_game_response(game)


@app.post(
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
    existing = await session.get(CasinoGameSettlement, game_id)
    if existing is not None:
        return to_game_response(existing)
    casino = await casino_account(session)
    amounts: dict[int, int] = {}
    for player in request.players:
        player_account = await require_account(
            session,
            player.identity.provider,
            identity_value(player.identity.value, player.identity.provider),
        )
        amounts[player_account.id] = amounts.get(player_account.id, 0) + player.amount
    if casino.id in amounts:
        raise HTTPException(status_code=422, detail="Casino cannot wager against itself")
    ids = sorted([*amounts, casino.id])
    accounts = (
        await session.scalars(
            select(Account).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()
        )
    ).all()
    by_id = {account.id: account for account in accounts}
    column = "money" if request.currency == "money" else "credits"
    if any(getattr(by_id[account_id], column) < amount for account_id, amount in amounts.items()):
        raise HTTPException(status_code=409, detail="INSUFFICIENT_FUNDS")
    total = sum(amounts.values())
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
    creator = await require_account(
        session,
        request.players[0].identity.provider,
        identity_value(request.players[0].identity.value, request.players[0].identity.provider),
    )
    game = CasinoGameSettlement(
        game_id=game_id,
        game_type=request.game_type,
        status="reserved",
        wager_currency=request.currency,
        wager_amount=total,
        created_by_account_id=creator.id,
        transaction_id=transaction.id,
    )
    session.add(game)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(CasinoGameSettlement, game_id)
        if existing is not None:
            return to_game_response(existing)
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    await session.refresh(game)
    return to_game_response(game)


@app.post(
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
        select(CasinoGameSettlement).where(CasinoGameSettlement.game_id == game_id).with_for_update()
    )
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if game.status != "reserved":
        return to_game_response(game)
    reserve_entries = (
        await session.scalars(
            select(TransactionEntry).where(TransactionEntry.transaction_id == game.transaction_id)
        )
    ).all()
    player_entries = [entry for entry in reserve_entries if entry.money_delta < 0 or entry.credits_delta < 0]
    winner = await require_account(
        session,
        request.winner.provider,
        identity_value(request.winner.value, request.winner.provider),
    )
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
    if casino_balance < payout:
        raise HTTPException(status_code=409, detail="CASINO_INSUFFICIENT_FUNDS")
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


@app.post(
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
        select(CasinoGameSettlement).where(CasinoGameSettlement.game_id == game_id).with_for_update()
    )
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if game.status != "reserved":
        return to_game_response(game)
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
            select(Account).where(Account.id.in_(account_ids)).order_by(Account.id).with_for_update()
        )
    ).all()
    by_id = {account.id: account for account in accounts}
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
