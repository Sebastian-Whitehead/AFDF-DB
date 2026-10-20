from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class IdentityRef(BaseModel):
    provider: Literal["steam", "discord"]
    value: str = Field(pattern=r"^\d{1,20}$")


class TransactionActor(BaseModel):
    identity: IdentityRef | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_actor(self) -> "TransactionActor":
        if (self.identity is None) == (self.label is None):
            raise ValueError("Provide exactly one of identity or label")
        return self


class AccountResponse(BaseModel):
    account_id: int
    account_type: str
    system_key: str | None
    steam_id: str | None
    discord_id: str | None
    money: int
    credits: int
    is_casino_account: bool
    created_at: datetime
    updated_at: datetime


class AdjustmentRequest(BaseModel):
    amount: int = Field(description="Positive to credit, negative to withdraw")
    reason: str = Field(min_length=1, max_length=500)
    reference_id: str = Field(min_length=1, max_length=255)
    actor: TransactionActor


class LeaderboardEntry(BaseModel):
    account_id: int
    steam_id: str | None
    discord_id: str | None
    balance: int


class LeaderboardResponse(BaseModel):
    currency: Literal["money", "credits"]
    entries: list[LeaderboardEntry]


class TransactionEntryResponse(BaseModel):
    account_id: int
    account_type: str
    system_key: str | None
    steam_id: str | None
    discord_id: str | None
    money_delta: int
    credits_delta: int


class TransactionResponse(BaseModel):
    id: UUID
    transaction_type: str
    reference_id: str | None
    description: str | None
    actor_account_id: int | None
    actor_steam_id: str | None
    actor_discord_id: str | None
    actor_label: str | None
    api_key_name: str | None = Field(
        description="Configured API client name; null for historical transactions"
    )
    api_key_fingerprint: str | None = Field(
        description="SHA-256 fingerprint of the API key used; null for historical transactions"
    )
    reversal_of_transaction_id: UUID | None
    created_at: datetime
    entries: list[TransactionEntryResponse]
    reversed_by_transaction_id: UUID | None = None


class ExchangeRequest(BaseModel):
    amount: int = Field(gt=0)
    reference_id: str = Field(min_length=1, max_length=255)
    expected_price_per_credit: int | None = Field(default=None, gt=0)
    actor: TransactionActor


class CasinoBindRequest(BaseModel):
    identity: IdentityRef


class LinkIdentityRequest(BaseModel):
    identity: IdentityRef


class PlayerWager(BaseModel):
    identity: IdentityRef
    amount: int = Field(gt=0)


class ReserveRequest(BaseModel):
    game_type: str = Field(min_length=1, max_length=64)
    currency: Literal["money", "credits"]
    players: list[PlayerWager] = Field(min_length=1)
    reference_id: str = Field(min_length=1, max_length=255)
    actor: TransactionActor


class SettleRequest(BaseModel):
    winner: IdentityRef
    result: dict[str, Any] = Field(default_factory=dict)
    actor: TransactionActor


class ReverseTransactionRequest(BaseModel):
    reference_id: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=500)
    actor: TransactionActor


class GameResponse(BaseModel):
    game_id: UUID
    game_type: str
    status: str
    wager_currency: str
    wager_amount: int
    created_by_account_id: int | None
    created_at: datetime
    settled_at: datetime | None
    result: dict[str, Any] | None
    transaction_id: UUID | None
