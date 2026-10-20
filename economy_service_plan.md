# Economy Service Data Design

## Purpose

The economy service is the authority for player balances, transfers, wagers, payouts, refunds, fees, and audit history. Discord and Steam clients identify the same player account through linked provider identities; balances and ledger records never key directly off an external ID.

## Identity rules

- Use `accounts.id` as the stable internal primary key for balances and foreign keys.
- Store provider IDs in `account_identities`, with provider values `steam` and `discord`.
- Steam identifiers are SteamID64 values; Discord identifiers are snowflakes. Both are accepted and returned as decimal strings so clients do not lose integer precision.
- An account may have at most one identity per provider. Each provider identity may belong to at most one account.
- Accounts may exist with only Steam or only Discord linked; linking an identity later resolves both to the same account.
- Casino is one current integration: its balance is an account with `account_type = 'casino'`. A partial unique index allows at most one casino account; casino program settings remain owned by that program and are not stored in this database. The account and ledger model is shared and is not casino-specific.

## Tables

### `accounts`

Stores the current balance projection:

```text
id                  BIGINT primary key
account_type        VARCHAR, normally "user", "system", or an integration-specific type such as "casino"
system_key          optional unique key for internal clearing accounts
money               BIGINT not null; non-system accounts cannot be negative
credits             BIGINT not null; non-system accounts cannot be negative
created_at          timestamp
updated_at          timestamp
```

### `account_identities`

```text
id                  integer primary key
account_id          BIGINT foreign key -> accounts.id
provider            "steam" or "discord"
identity_value      decimal ID string
created_at          timestamp
UNIQUE(provider, identity_value)
UNIQUE(account_id, provider)
```

Never merge accounts implicitly. A link request must fail if the identity belongs to another account or the target account already has a different identity for that provider.

### `transactions`

One immutable header per financial operation:

```text
id                          UUID primary key
idempotency_key             unique string
transaction_type            string
reference_id                optional string
actor_account_id            optional foreign key -> accounts.id
actor_label                 optional service/admin label
api_key_fingerprint        optional SHA-256 fingerprint of the API key used
description                 optional reason/details
reversal_of_transaction_id  optional unique self-reference
created_at                  timestamp
```

The actor is either a linked account or an explicit service/admin label. Never rewrite or delete a posted transaction.

New transactions record the configured API client name and a SHA-256 fingerprint of the API key that submitted the request. Each client is explicitly configured as a `name=token` pair in `API_CLIENTS`. This lets operators identify the integration and correlate activity without storing or returning the secret itself. Historical transactions have no client name or fingerprint.

### `transaction_entries`

Persisted columns:

```text
id                  integer primary key
transaction_id      UUID foreign key -> transactions.id
account_id          BIGINT foreign key -> accounts.id
money_delta         BIGINT
credits_delta       BIGINT
```

Entries identify affected accounts and signed deltas: negative is a debit, positive is a credit. Account-to-account transactions have matching debit and credit entries. Adjustments and credit exchanges use system clearing accounts as the other side, so the log identifies the source and destination rather than only the player balance change. System clearing balances may be negative; other account balances may not.

The API enriches each entry with the account type, system key, and currently linked Steam and Discord IDs. These are resolved from `accounts` and `account_identities` when returning the response; they are not stored in `transaction_entries`. Persistent references use internal account IDs so identity links can change without rewriting history.

### `casino_game_settlements`

The current casino integration's game settlement records reference internal account and transaction IDs. The shared economy service does not have a settings table.

## Reversal policy

A reversal is a new compensating transaction whose entries are the exact opposite of the original entries. `reversal_of_transaction_id` uniquely references the original transaction, allowing at most one full reversal. The operation locks affected accounts, checks that no balance becomes negative, updates balances and appends the reversal in one database transaction. The original transaction remains unchanged.

Game refunds follow the same compensating-entry principle and also transition the game state to `refunded`. A game state transition must not be bypassed by editing ledger rows directly.

## API shape

Identity-qualified account routes use:

```text
/api/v1/accounts/steam/{steam_id64}
/api/v1/accounts/discord/{discord_snowflake}
```

Create an account idempotently with `PUT`, read it with `GET`, and link its second identity through:

```text
POST /api/v1/accounts/{provider}/{identity}/identities
{"identity": {"provider": "discord", "value": "123456789012345678"}}
```

Balance mutations include an idempotency `reference_id` and actor identity or service/admin label. Transaction history is returned for an account identity, with entry deltas and all linked IDs. A reversal endpoint appends one full compensating transaction. Game wagers and winners reference provider-qualified identities and are resolved to internal account IDs before balance changes.

See [API_REFERENCE.md](./API_REFERENCE.md) for full endpoint contracts.

## Migration and legacy data

Alembic revision `0002_account_identities` converts existing service `external_user_id` values to Discord identities, preserving existing PostgreSQL accounts and balances. It also converts actor/game creator references to internal account IDs and adds system clearing accounts. The migration is intentionally not reversible; take a database backup before applying it.

Alembic revision `0003_remove_settings` migrates the former casino account binding into `accounts.account_type = 'casino'`, drops the economy `settings` table, and enforces at most one casino account.

Alembic revision `0004_casino_game_settlements` renames the casino-specific `game_settlements` table to `casino_game_settlements`. Shared balance and ledger tables remain generically named.

Alembic revision `0005_api_key_audit` adds the nullable API-key fingerprint field to transaction headers.

Alembic revision `0006_api_key_names` adds the nullable API client name to transaction headers. Existing transactions remain valid and have no retroactively assigned client name or fingerprint. This is the current schema revision.

The checked-in `economy.db` contains only Discord IDs and no Steam mappings. The user chose not to carry those current legacy balances forward. Do not run the SQLite importer for that file. The importer requires an explicit `--confirm-import` flag for any future deliberate import and rejects non-integer balances.

## Operational invariants

- All balance changes and ledger entries commit atomically.
- Lock affected account rows in deterministic internal-ID order.
- Never allow player balances to become negative.
- Use database uniqueness constraints for identity ownership, idempotency, and one-time reversal.
- Calculate game payouts and fees on the service, not in the client.
- Keep PostgreSQL private to the API container.
