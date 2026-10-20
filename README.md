# General-Purpose Economy Service

Standalone FastAPI service for account identities, balances, transfers, and an auditable transaction ledger. It is designed as shared economy infrastructure for multiple programs, not just the casino. PostgreSQL is private to the API container; clients use authenticated HTTP.

## Account identities and ledger

An account has a stable internal `account_id` and may be linked to one SteamID64, one Discord snowflake, or both. IDs are represented as decimal strings in API requests and responses to avoid loss of precision in clients. Each provider ID can belong to only one account. Link the second identity using:

```text
POST /api/v1/accounts/{provider}/{identity}/identities
{"identity": {"provider": "discord", "value": "123456789012345678"}}
```

`transactions` stores the actor, reason/reference, and reversal relationship. `transaction_entries` records signed deltas for each affected account; transactions use system clearing accounts as counterparts for external adjustments and credit exchange, making the source and destination visible. Transactions are append-only; reverse an eligible transaction by creating one full compensating transaction. A transaction can only be reversed once.

See [API_REFERENCE.md](./API_REFERENCE.md) for request and response formats.

## Casino integration

The service currently includes casino-specific binding and game workflows alongside its shared account and ledger API. Casino game lifecycle data is isolated in `casino_game_settlements`; balances and financial transactions use the generic economy tables. Casino application settings remain owned by the casino program.

## Run with Docker

1. Copy `.env.example` to `.env`, then set a strong `POSTGRES_PASSWORD` and `API_KEYS`.
2. Start the service:

```text
docker compose up --build
```

The container applies Alembic migrations before starting the API. API docs are at `http://localhost:8000/docs`; protected endpoints require `X-API-Key`.

## Legacy SQLite import

The checked-in `economy.db` contains Discord-keyed records and no Steam mapping. To preserve identity integrity, the importer is explicit and will not run without `--confirm-import`. You previously chose not to carry those legacy balances forward, so do not run this command for the current database:

```text
python -m scripts.migrate_sqlite --sqlite-path economy.db --confirm-import
```

If intentionally importing another legacy database, back it up first and verify every balance. The importer rejects non-integer balances and links imported accounts to their Discord snowflakes; Steam IDs can be linked later.

## Local development

```text
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[test]"
alembic upgrade head
uvicorn app.main:app --reload
```

Set `DATABASE_URL` and `API_KEYS` in `.env`. Casino-specific settings are owned by the casino program; its balance is represented as a typed account in the shared economy ledger.
