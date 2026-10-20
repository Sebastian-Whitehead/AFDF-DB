# Economy Service API

This API provides shared account, balance, and transaction-ledger operations. The casino routes and game workflows below are one integration built on that general-purpose economy service.

## Conventions

- Base URL: `http://localhost:8000`
- Protected routes require `X-API-Key`. Clients listed in `API_READ_ONLY_CLIENTS` may call read endpoints only; other configured clients retain read/write access.
- Account linking that can merge accounts and account identity recovery require a client listed in `API_ADMIN_CLIENTS`.
- Configure clients as explicit `name=token` pairs in `API_CLIENTS` (tokens must not contain commas). Names appear in transaction audit history; the raw token is never stored.
- `API_READ_ONLY_CLIENTS` and `API_ADMIN_CLIENTS` contain client names, not token values. Admin clients must also be configured in `API_CLIENTS` and cannot be read-only.
- External IDs are ASCII decimal strings. Steam identities must be individual SteamID64 values (`76561197960265728`–`76561202255233023`); Discord identities must be nonzero unsigned 64-bit snowflakes. `account_id` is the service's internal integer key.
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

Health endpoints are public. Protected `GET` endpoints require a valid API key and allow read-only clients. Protected mutations require a write-capable client; account linking and migration additionally require a client named in `API_ADMIN_CLIENTS`. Configure `API_READ_ONLY_CLIENTS` and `API_ADMIN_CLIENTS` as comma-separated lists of names from `API_CLIENTS`. Admin clients cannot be read-only.

## Accounts and identity links

```text
GET /api/v1/accounts/{provider}/{identity}
PUT /api/v1/accounts/{provider}/{identity}
POST /api/v1/accounts/{provider}/{identity}/identities
POST /api/v1/accounts/{provider}/{identity}/migrate
GET /api/v1/accounts/{provider}/{identity}/transactions?limit=50
POST /api/v1/transfers
```

`PUT` creates a zero-balance account if the identity is not already linked. To associate a Steam ID with a Discord ID, use the identity already linked to the account in the URL and provide the other identity in the body. The operation works in either direction:

```http
POST /api/v1/accounts/steam/76561198000000000/identities
X-API-Key: <trusted-admin-token>
Content-Type: application/json

{
  "identity": {
    "provider": "discord",
    "value": "123456789012345678"
  }
}
```

For a local server, the equivalent command is:

```sh
curl -X POST "http://localhost:8000/api/v1/accounts/steam/76561198000000000/identities" \
  -H "X-API-Key: <trusted-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"identity":{"provider":"discord","value":"123456789012345678"}}'
```

To link Steam starting from Discord, reverse the URL and body providers:

```http
POST /api/v1/accounts/discord/123456789012345678/identities
X-API-Key: <trusted-admin-token>
Content-Type: application/json

{"identity": {"provider": "steam", "value": "76561198000000000"}}
```

The identity in the URL must already resolve to a user account. If neither identity has an account, first create one using `PUT /api/v1/accounts/{provider}/{identity}` for the identity you want to use as the starting account. Do not `PUT` the second identity merely to link it: if it is already linked to a separate account, the link operation merges instead.

If the supplied identity is already linked to a different user account, the API merges that account into the URL account and returns the surviving account. Balances are added; identities and references in transaction history and casino games are reassigned to the surviving account. If merging would produce two different identities for the same provider, the request returns `409` without changing either account. System and casino accounts cannot be merged.

On success, the response is an `AccountResponse`. Verify the association by looking up the account with either provider-qualified ID:

```text
GET /api/v1/accounts/steam/76561198000000000
GET /api/v1/accounts/discord/123456789012345678
```

Both requests require a valid API key and should return the same `account_id`. The link request requires a client in `API_ADMIN_CLIENTS`; an ordinary write key gets `403`. An unknown URL identity returns `404`; incompatible or protected account merges return `409`.

These account-changing operations require an API client listed in `API_ADMIN_CLIENTS`, not merely a write-capable client.

### Recovering a lost provider identity

Use the migration route to replace an old identity with a new identity from the same provider:

```text
POST /api/v1/accounts/discord/123456789012345678/migrate
X-API-Key: <account-admin-token>
Content-Type: application/json
```

```json
{"identity": {"provider": "discord", "value": "234567890123456789"}}
```

If the destination identity is not registered, the existing identity value is replaced in place and the account balance/history remain on that account. If the destination identity already belongs to a different user account, that account survives: the old identity is removed, compatible identities and historical references are moved, and balances are combined. Provider conflicts, balance overflow, or non-user accounts return `409`; no partial migration is committed.

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

Transfer request:

```json
{
  "sender": {"provider": "steam", "value": "76561198000000000"},
  "recipient": {"provider": "discord", "value": "123456789012345678"},
  "currency": "money",
  "amount": 10,
  "reference_id": "payment-123"
}
```

Transfers move one currency between two distinct existing accounts in a single balanced ledger transaction. The sender is recorded as the transaction actor. Retrying the same reference with the same sender, recipient, currency, and amount returns the original transaction; changing any of those values returns `409`.

## Transaction history and reversals

Transaction history returns each transaction touching the selected account. Each transaction contains its actor and entries with the affected account IDs, linked Steam/Discord IDs, account type, system clearing key (if applicable), and signed money/credit deltas. Negative deltas are debits; positive deltas are credits. Transfers have corresponding debit and credit entries; adjustments/exchanges use system clearing accounts such as `external_money` and `external_credits` to make their other side explicit. Results are ordered newest first by creation time and transaction UUID. Use the last transaction's `id` as `before` to fetch the next page, for example `?limit=50&before=<uuid>`; the cursor must belong to the selected account.

Each new transaction includes `api_key_name` and `api_key_fingerprint`, identifying the configured client and a stable `sha256:`-prefixed fingerprint of the API token that submitted the operation. For example, a transaction may show `api_key_name: "casino-bot"`. The raw token is never stored in the transaction or returned by the API. Transactions created before these audit fields were added return `null`.

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

Reservation and settlement use internal account IDs in the database, regardless of which linked identity the client submits. The service computes configured fees. Refunds create compensating ledger entries and mark the game refunded. An identical reserve retry or settled-game retry returns the original result; a retry that changes the reservation, winner, or settlement result returns `409`. A settled game cannot be refunded, and a refunded game cannot be settled. Repeated refund requests remain idempotent.
