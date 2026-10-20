from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("account_type = 'system' OR money >= 0"),
        CheckConstraint("account_type = 'system' OR credits >= 0"),
        Index(
            "uq_accounts_single_casino",
            "account_type",
            unique=True,
            postgresql_where=text("account_type = 'casino'"),
            sqlite_where=text("account_type = 'casino'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_type: Mapped[str] = mapped_column(String(32), default="user")
    system_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    money: Mapped[int] = mapped_column(BigInteger, default=0)
    credits: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    entries: Mapped[list[TransactionEntry]] = relationship(back_populates="account")
    identities: Mapped[list[AccountIdentity]] = relationship(back_populates="account", cascade="all, delete-orphan")


class AccountIdentity(Base):
    __tablename__ = "account_identities"
    __table_args__ = (
        CheckConstraint("provider IN ('steam', 'discord')"),
        UniqueConstraint("provider", "identity_value"),
        UniqueConstraint("account_id", "provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    identity_value: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    account: Mapped[Account] = relationship(back_populates="identities")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    transaction_type: Mapped[str] = mapped_column(String(64))
    reference_id: Mapped[str | None] = mapped_column(String(255))
    actor_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    actor_label: Mapped[str | None] = mapped_column(String(255))
    api_key_name: Mapped[str | None] = mapped_column(String(64))
    api_key_fingerprint: Mapped[str | None] = mapped_column(String(71))
    description: Mapped[str | None] = mapped_column(Text)
    reversal_of_transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("transactions.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    entries: Mapped[list[TransactionEntry]] = relationship(back_populates="transaction", cascade="all, delete-orphan")


class TransactionEntry(Base):
    __tablename__ = "transaction_entries"
    __table_args__ = (CheckConstraint("money_delta <> 0 OR credits_delta <> 0"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[UUID] = mapped_column(ForeignKey("transactions.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    money_delta: Mapped[int] = mapped_column(BigInteger, default=0)
    credits_delta: Mapped[int] = mapped_column(BigInteger, default=0)
    transaction: Mapped[Transaction] = relationship(back_populates="entries")
    account: Mapped[Account] = relationship(back_populates="entries")


class CasinoGameSettlement(Base):
    __tablename__ = "casino_game_settlements"

    game_id: Mapped[UUID] = mapped_column(primary_key=True)
    game_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    wager_currency: Mapped[str] = mapped_column(String(16))
    wager_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    created_by_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("transactions.id"))
