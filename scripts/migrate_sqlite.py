import argparse
import asyncio
import sqlite3

from sqlalchemy import select

from app.db import engine, session_factory
from app.models import Account, AccountIdentity, Transaction, TransactionEntry


async def migrate(sqlite_path: str, confirm_import: bool) -> None:
    if not confirm_import:
        raise RuntimeError("Pass --confirm-import to explicitly import legacy balances")

    with sqlite3.connect(sqlite_path) as source:
        rows = source.execute("SELECT user_id, money, credits FROM economy ORDER BY user_id").fetchall()

    for user_id, money, credits in rows:
        if int(money) != money or int(credits) != credits:
            raise RuntimeError(f"Account {user_id} has a non-integer balance; resolve it before importing")

    async with session_factory() as session:
        for user_id, money, credits in rows:
            identity_value = str(user_id)
            identity = await session.scalar(
                select(AccountIdentity).where(
                    AccountIdentity.provider == "discord",
                    AccountIdentity.identity_value == identity_value,
                )
            )
            if identity is not None:
                existing = await session.get(Account, identity.account_id)
                if existing is None:
                    raise RuntimeError(f"Discord identity {user_id} references a missing account")
                if (existing.money, existing.credits) != (money, credits):
                    raise RuntimeError(f"Account {user_id} already exists with different balances")
                continue

            account = Account(money=money, credits=credits)
            account.identities.append(
                AccountIdentity(provider="discord", identity_value=identity_value)
            )
            transaction = Transaction(
                idempotency_key=f"legacy-sqlite-v2:{user_id}",
                transaction_type="migration",
                reference_id=f"legacy-sqlite-v2:{user_id}",
                actor_label="legacy_sqlite_import",
            )
            if money or credits:
                transaction.entries.append(
                    TransactionEntry(
                        account=account,
                        money_delta=money,
                        credits_delta=credits,
                    )
                )
                external_money = await session.scalar(
                    select(Account).where(Account.system_key == "external_money")
                )
                external_credits = await session.scalar(
                    select(Account).where(Account.system_key == "external_credits")
                )
                if external_money is None or external_credits is None:
                    raise RuntimeError("System ledger accounts are missing; apply Alembic migrations first")
                if money:
                    external_money.money -= money
                    transaction.entries.append(
                        TransactionEntry(account=external_money, money_delta=-money)
                    )
                if credits:
                    external_credits.credits -= credits
                    transaction.entries.append(
                        TransactionEntry(account=external_credits, credits_delta=-credits)
                    )
            session.add_all([account, transaction])
        await session.commit()

    await engine.dispose()
    print(f"Migrated {len(rows)} accounts with Discord identities")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument(
        "--confirm-import",
        action="store_true",
        help="Explicitly allow importing the legacy Discord-keyed balances",
    )
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite_path, args.confirm_import))
