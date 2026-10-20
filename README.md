# General-Purpose Economy Service

Standalone FastAPI service for account identities, balances, transfers, and an auditable transaction ledger. It is designed as shared economy infrastructure for multiple programs, not just the casino. PostgreSQL is private to the API container; clients use authenticated HTTP.

## Account identities and ledger

An account has a stable internal `account_id` and may be linked to one SteamID64, one Discord snowflake, or both. IDs are represented as ASCII decimal strings in API requests and responses to avoid loss of precision in clients. Steam identities must be individual SteamID64 values (`76561197960265728`–`76561202255233023`); Discord snowflakes must be nonzero unsigned 64-bit values. Each provider ID can belong to only one account. Link the second identity using:

```text
POST /api/v1/accounts/{provider}/{identity}/identities
```

The URL identity must already resolve to an account. A request can start from Steam or Discord; the body supplies the other provider's ID. Use an API client listed in `API_ADMIN_CLIENTS`. If the supplied ID already belongs to another user account, that account is merged into the URL account: balances are summed and history is reassigned. See [API_REFERENCE.md](./API_REFERENCE.md) for copyable requests and response details.

`transactions` stores the actor, reason/reference, and reversal relationship. `transaction_entries` records signed deltas for each affected account; transactions use system clearing accounts as counterparts for external adjustments and credit exchange, making the source and destination visible. Transactions are append-only; reverse an eligible transaction by creating one full compensating transaction. A transaction can only be reversed once.

New transaction records include the configured API client name and a SHA-256 fingerprint of the key used to submit them. Configure `API_CLIENTS` using explicit `name=token` pairs (tokens must not contain commas). This identifies the caller for audit purposes without storing the secret itself.

Identity association can merge separate user accounts, and the identity migration endpoint supports provider-account recovery. Both operations are restricted to clients listed in `API_ADMIN_CLIENTS`; keep that scope to trusted integrations and administrators.

See [API_REFERENCE.md](./API_REFERENCE.md) for request and response formats.

## Application structure

The API is organized as domain routers inside one FastAPI service. `app/main.py` assembles the app and includes the account, transaction, and casino routers. HTTP handlers live in `app/routers/accounts.py`, `app/routers/transactions.py`, and `app/routers/casino.py`; shared account lookup, locking, ledger serialization, and ledger-entry helpers live in `app/services.py`. The routers keep their existing `/api/v1/...` paths and use the same database session, so operations that update balances and ledger entries remain atomic. This is an in-process modular structure, not a separately deployed casino service.

## Casino integration

The service currently includes casino-specific binding and game workflows alongside its shared account and ledger API. Casino game lifecycle data is isolated in `casino_game_settlements`; balances and financial transactions use the generic economy tables. Casino application settings remain owned by the casino program.

## Run with Docker

1. Copy `.env.example` to `.env`. Set a strong `POSTGRES_PASSWORD` and replace the API token placeholders with unique secrets. `API_CLIENTS` uses `client-name=token` pairs. `API_READ_ONLY_CLIENTS` is a list of client names that can only call protected `GET` endpoints; `API_ADMIN_CLIENTS` is a list of trusted clients allowed to link or migrate identities. Never grant admin scope to a read-only client, and never commit `.env`.
2. Start the service:

```text
docker compose up --build
```

The container applies Alembic migrations before starting the API. API docs are at `http://localhost:8000/docs`; protected endpoints require `X-API-Key`. Restart the service after changing `.env` so the new client settings take effect. Read-only keys can access `GET` routes but are denied `PUT` and `POST` routes.

### Link a Steam ID and Discord ID

If one ID already identifies the account, send the other ID to the identity-link endpoint using an admin client's token:

```text
POST http://localhost:8000/api/v1/accounts/steam/76561198000000000/identities
X-API-Key: <trusted-admin-token>
Content-Type: application/json

{"identity": {"provider": "discord", "value": "123456789012345678"}}
```

The request returns the resulting account. To start with a Discord account instead, use its Discord ID in the URL and provide the Steam ID in the body. If neither ID has an account yet, first create one with `PUT /api/v1/accounts/{provider}/{identity}`, then link the other ID. Avoid creating a second account just to link an identity; linking an ID that already has a separate user account will merge those accounts, sum their balances, and retain the URL account as the survivor.

After linking, either provider-qualified lookup returns the same account:

```text
GET http://localhost:8000/api/v1/accounts/steam/76561198000000000
GET http://localhost:8000/api/v1/accounts/discord/123456789012345678
```

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

Set `DATABASE_URL` and `API_CLIENTS` in `.env`. Casino-specific settings are owned by the casino program; its balance is represented as a typed account in the shared economy ledger.
