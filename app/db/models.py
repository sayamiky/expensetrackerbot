from datetime import date, datetime, timezone

from sqlalchemy import JSON, BigInteger, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Money columns are whole rupiah in BigInteger — never Float, which cannot
# represent amounts exactly.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Receipt(Base):
    """One transaction: a scanned receipt or a typed expense.

    A photo starts as status="pending" until the user taps Simpan; a typed
    expense is saved as "confirmed" straight away. Reports only count
    confirmed rows.
    """

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Telegram chat id; group chat ids are negative, hence BigInteger.
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Local (WITA) date the purchase happened; what every report groups by.
    transaction_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    subtotal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IDR")
    payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # The category with the largest share of the total; per-item categories
    # live on ReceiptItem and are what reports actually sum.
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "photo" or "text" — how the user reported this transaction.
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    # "pending" or "confirmed".
    status: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    # The photo stays on Telegram's servers; its file_id is enough to fetch it again.
    telegram_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # What the model actually returned, for debugging and prompt tuning.
    raw_llm_json: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    items: Mapped[list["ReceiptItem"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ReceiptItem.id",
    )


class ReceiptItem(Base):
    """One line of a receipt, with its own category."""

    __tablename__ = "receipt_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The line total as printed on the receipt.
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # This line's share of the receipt total after receipt-level discount and
    # tax, so the net amounts of a receipt always sum exactly to its total.
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(30), index=True, nullable=False)

    receipt: Mapped[Receipt] = relationship(back_populates="items")


class Budget(Base):
    """A chat's monthly spending limit for one category."""

    __tablename__ = "budgets"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category: Mapped[str] = mapped_column(String(30), primary_key=True)
    monthly_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ProcessedUpdate(Base):
    """Telegram update ids already handled.

    Telegram re-sends an update when the webhook is slow to answer; without
    this table a retried receipt would be logged twice.
    """

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ErrorLog(Base):
    """A failure the webhook caught, so it is visible from /logs, not just
    Vercel's console (which is easy to miss and short-retention)."""

    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable: a handful of failures happen before a chat_id is known.
    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
