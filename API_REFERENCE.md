# Economy Service API

This API provides shared account, balance, and transaction-ledger operations. The casino routes and game workflows below are one integration built on that general-purpose economy service.

## Conventions

- Base URL: `http://localhost:8000`
- Protected routes require `X-API-Key`.
- External IDs are decimal strings. `steam` means SteamID64; `discord` means a Discord snowflake. `account_id` is the service's internal integer key.
- A player account can have one Steam identity, one Discord identity, either, or both. Provider IDs are unique across accounts.
- Balance mutations use integer amounts and unique `reference_id` values for idempotency.

Identity path examples:

```text
/api/v1/accounts/steam/76561198000000000
/api/v1/accounts/discord/123456789012345678
```

## Health

```text
GET /health/live
GET /health/ready
```

## Accounts and identity links

```text
GET /api/v1/accounts/{provider}/{identity}
PUT /api/v1/accounts/{provider}/{identity}
POST /api/v1/accounts/{provider}/{identity}/identities
GET /api/v1/accounts/{provider}/{identity}/transactions?limit=50
```

`PUT` creates a zero-balance account if the identity is not already linked. To link a second ID to the same player account:

```json
{
  "identity": {
    "provider": "discord",
    "value": "123456789012345678"
  }
}
```

The identity path must already resolve to an account. Linking an identity that belongs to another account, or replacing an existing provider identity, returns `409`.

Account response:

```json
{
  "account_id": 42,
  "account_type": "user",
  "system_key": null,
  "steam_id": "76561198000000000",
  "discord_id": "123456789012345678",
  "money": 100,
  "credits": 25,
  "is_casino_account": false,
  "created_at": "...",
  "updated_at": "..."
}
```

## Balance adjustments and credit exchange

```text
POST /api/v1/accounts/{provider}/{identity}/money/adjust
POST /api/v1/accounts/{provider}/{identity}/credits/adjust
POST /api/v1/accounts/{provider}/{identity}/credits/purchase
POST /api/v1/accounts/{provider}/{identity}/credits/sell
```

Adjustment example:

```json
{
  "amount": 100,
  "reason": "manual correction",
  "reference_id": "admin-action-123",
  "actor": {"label": "economy-admin"}
}
```

The actor must identify either an existing account identity or a service/admin label:

```json
{"identity": {"provider": "steam", "value": "76561198000000000"}}
```

Negative adjustments fail if the account lacks funds. Credit exchange uses the configured `TOKEN_PRICE`; `expected_price_per_credit` is optional and detects stale client pricing.

## Transaction history and reversals

Transaction history returns each transaction touching the selected account. Each transaction contains its actor and entries with the affected account IDs, linked Steam/Discord IDs, account type, system clearing key (if applicable), and signed money/credit deltas. Negative deltas are debits; positive deltas are credits. Transfers have corresponding debit and credit entries; adjustments/exchanges use system clearing accounts such as `external_money` and `external_credits` to make their other side explicit.

```text
POST /api/v1/transactions/{transaction_id}/reverse
```

Reversal request:

```json
{
  "reference_id": "reverse-admin-action-123",
  "reason": "Original adjustment was incorrect",
  "actor": {"label": "economy-admin"}
}
```

Reversals never edit or delete the original transaction. A reversal is a new transaction with the original entry deltas negated; each transaction can be fully reversed at most once. The reversal fails if it would make any non-system account balance negative or if the original ledger entries are unbalanced. The latter prevents older one-sided transactions from being reversed as if they had a recorded source/destination.

## Casino integration

```text
GET /api/v1/casino/balance
POST /api/v1/casino/bind
```

Bind a casino account to an existing or new identity:

```json
{"identity": {"provider": "discord", "value": "123456789012345678"}}
```

The economy service marks the selected row with `account_type = "casino"`; a partial unique index allows at most one casino account. A later bind to another account returns `409`. The casino program owns its own settings; the economy database has no settings table.

The casino-specific game lifecycle table is named `casino_game_settlements`. Accounts and transaction-ledger tables are shared economy data and keep generic table names.

## Leaderboard

```text
GET /api/v1/leaderboard?currency=money&limit=10
```

The shared leaderboard supports `money` or `credits` and excludes system clearing accounts and the casino account.

## Games

```text
POST /api/v1/games/{game_id}/reserve
POST /api/v1/games/{game_id}/settle
POST /api/v1/games/{game_id}/refund
GET /api/v1/games/{game_id}
```

Reserve example:

```json
{
  "game_type": "coinflip",
  "currency": "money",
  "players": [
    {"identity": {"provider": "steam", "value": "76561198000000000"}, "amount": 10},
    {"identity": {"provider": "discord", "value": "234567890123456789"}, "amount": 10}
  ],
  "reference_id": "discord-interaction-123",
  "actor": {"label": "game-service"}
}
```

Settlement:

```json
{
  "winner": {"provider": "steam", "value": "76561198000000000"},
  "result": {"coin": "heads"},
  "actor": {"label": "game-service"}
}
```

Reservation and settlement use internal account IDs in the database, regardless of which linked identity the client submits. The service computes configured fees. Refunds create compensating ledger entries and mark the game refunded. Repeated reserve/settle/refund requests do not pay twice.
