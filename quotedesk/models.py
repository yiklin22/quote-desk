"""Database tables. Money is NUMERIC and maps to Decimal."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(12, 2)


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """One login, and the owner of everything else. Every enquiry, quote and
    price list belongs to exactly one account and is deleted with it."""
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LoginSession(Base):
    """A browser session. Only the SHA-256 of the cookie value is stored."""
    __tablename__ = "login_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginFailure(Base):
    """A failed login, kept for the throttling window. `key` is "email:..." or "ip:..."."""
    __tablename__ = "login_failures"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(300), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class UsageCounter(Base):
    """How much of a daily allowance an account has used. Not reduced by deleting data."""
    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("account_id", "day", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(30))
    count: Mapped[int] = mapped_column(Integer, default=0)


class PriceListVersion(Base):
    """Immutable. Changing prices means publishing a new version; quotes
    keep pointing at the version they were priced with. Each account has
    its own versions, numbered from 1."""
    __tablename__ = "price_list_versions"
    __table_args__ = (UniqueConstraint("account_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Enquiry(Base):
    __tablename__ = "enquiries"
    __table_args__ = (UniqueConstraint("account_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)                # 1, 2, 3... within the account; used in quote numbers
    text: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(10))            # email | form
    extractor: Mapped[str] = mapped_column(String(40))
    requirements: Mapped[dict] = mapped_column(JSON)            # domain.Requirements
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    quote: Mapped[Quote | None] = relationship(back_populates="enquiry", cascade="all, delete-orphan", uselist=False)
    reply: Mapped[ReplyDraft | None] = relationship(cascade="all, delete-orphan", uselist=False)
    events: Mapped[list[Event]] = relationship(back_populates="enquiry", cascade="all, delete-orphan", order_by="Event.id")


class Quote(Base):
    """One per enquiry. `revision` goes up whenever anything a price depends
    on changes. An approval is for one revision only."""
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(ForeignKey("enquiries.id", ondelete="CASCADE"), unique=True)
    price_list_id: Mapped[int] = mapped_column(ForeignKey("price_list_versions.id"))
    priced_on: Mapped[date] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    adjustments: Mapped[dict] = mapped_column(JSON, default=dict)   # domain.Adjustments
    subtotal: Mapped[Decimal] = mapped_column(MONEY)
    surcharge: Mapped[Decimal] = mapped_column(MONEY)
    discount: Mapped[Decimal] = mapped_column(MONEY)
    gst: Mapped[Decimal] = mapped_column(MONEY)
    total: Mapped[Decimal] = mapped_column(MONEY)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    enquiry: Mapped[Enquiry] = relationship(back_populates="quote")
    price_list: Mapped[PriceListVersion] = relationship()
    lines: Mapped[list[QuoteLine]] = relationship(back_populates="quote", cascade="all, delete-orphan", order_by="QuoteLine.position")
    approvals: Mapped[list[Approval]] = relationship(back_populates="quote", cascade="all, delete-orphan", order_by="Approval.id")


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    __table_args__ = (UniqueConstraint("quote_id", "position"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    kind: Mapped[str] = mapped_column(String(12))
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    quote: Mapped[Quote] = relationship(back_populates="lines")


class Approval(Base):
    """A person approved exactly this revision at exactly this total."""
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    total: Mapped[Decimal] = mapped_column(MONEY)
    approved_by: Mapped[str] = mapped_column(String(80))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    quote: Mapped[Quote] = relationship(back_populates="approvals")


class ReplyDraft(Base):
    """A model's wording of the reply. Shown only while the standard reply
    it was written from is unchanged, so any edit to the quote retires it."""
    __tablename__ = "reply_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(ForeignKey("enquiries.id", ondelete="CASCADE"), unique=True)
    source_hash: Mapped[str] = mapped_column(String(64))        # sha256 of the standard reply it reworded
    text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    """Append-only history of what was done to an enquiry and by whom."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    enquiry_id: Mapped[int] = mapped_column(ForeignKey("enquiries.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    enquiry: Mapped[Enquiry] = relationship(back_populates="events")
