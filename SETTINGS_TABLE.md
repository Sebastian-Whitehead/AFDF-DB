# Former Economy Service `settings` Table

This file documents the economy service's former `settings` table before its removal. It describes the schema and setting understood by the service code; it is not a dump of rows from a live PostgreSQL database.

## Former schema

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `key` | `VARCHAR(64)` | Primary key | Setting name |
| `value` | `VARCHAR(255)` | Not null | Setting value, stored as text |
| `updated_at` | timezone-aware timestamp | Not null, defaults to database current time; updated on ORM updates | Last update time |

## Economy-service setting

| Key | Value format | Purpose |
|---|---|---|
| `casino_external_user_id` | Discord snowflake, serialized as a decimal string | Identified the casino account by its Discord identity |
| `casino_account_id` | Internal `accounts.id`, serialized as a decimal string | Later internal-ID form of the casino account binding |

The table model only defines generic `key`, `value`, and `updated_at` columns; the keys above are the casino-binding values consumed by the service code. This file documents those known setting formats, not actual row values from a live database. The economy service now marks the casino balance row with `accounts.account_type = 'casino'`, so it no longer needs this table or setting.

## Settings ownership

Configuration and settings for each casino program should be stored and managed by that individual program. This document is retained only as a record of the removed economy-service table and its former content contract.
