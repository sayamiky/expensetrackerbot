"""Database access for receipts: create, confirm, edit, delete, report."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Receipt, ReceiptItem
from app.schemas.expense import Category
from app.services.expense_check import CheckedExpense

PENDING = "pending"
CONFIRMED = "confirmed"


@dataclass
class PeriodSummary:
    total: int
    count: int
    # (category slug, amount), largest first.
    by_category: list[tuple[str, int]]


async def create_receipt(
    session: AsyncSession,
    chat_id: int,
    checked: CheckedExpense,
    *,
    source: str,
    status: str,
    telegram_file_id: str | None = None,
    raw_llm_json: dict | None = None,
    confidence: float | None = None,
    note: str | None = None,
) -> Receipt:
    receipt = Receipt(
        chat_id=chat_id,
        merchant=checked.merchant,
        transaction_date=checked.transaction_date,
        subtotal=checked.subtotal,
        discount=checked.discount,
        tax=checked.tax,
        total=checked.total,
        payment_method=checked.payment_method,
        category=checked.category.value,
        note=note,
        source=source,
        status=status,
        telegram_file_id=telegram_file_id,
        raw_llm_json=raw_llm_json,
        confidence=confidence,
        items=[
            ReceiptItem(
                name=item.name,
                qty=item.qty,
                unit_price=item.unit_price,
                total=item.total,
                net_amount=net,
                category=item.category.value,
            )
            for item, net in zip(checked.items, checked.net_amounts)
        ],
    )
    session.add(receipt)
    await session.commit()
    return receipt


async def get_receipt(session: AsyncSession, chat_id: int, receipt_id: int) -> Receipt | None:
    """A receipt, only if it belongs to ``chat_id`` — never another chat's."""
    result = await session.execute(
        select(Receipt).where(Receipt.id == receipt_id, Receipt.chat_id == chat_id)
    )
    return result.scalar_one_or_none()


async def confirm_receipt(session: AsyncSession, chat_id: int, receipt_id: int) -> Receipt | None:
    """Mark a draft as confirmed. Idempotent: confirming twice is harmless."""
    receipt = await get_receipt(session, chat_id, receipt_id)
    if receipt is None:
        return None
    receipt.status = CONFIRMED
    await session.commit()
    return receipt


async def delete_receipt(session: AsyncSession, chat_id: int, receipt_id: int) -> bool:
    receipt = await get_receipt(session, chat_id, receipt_id)
    if receipt is None:
        return False
    await session.delete(receipt)
    await session.commit()
    return True


async def set_category(
    session: AsyncSession, chat_id: int, receipt_id: int, category: Category
) -> Receipt | None:
    """Move every item of a receipt to one category."""
    receipt = await get_receipt(session, chat_id, receipt_id)
    if receipt is None:
        return None
    for item in receipt.items:
        item.category = category.value
    receipt.category = category.value
    await session.commit()
    return receipt


async def list_receipts_for_day(session: AsyncSession, chat_id: int, day: date) -> list[Receipt]:
    """Confirmed receipts dated ``day``, oldest first."""
    result = await session.execute(
        select(Receipt)
        .where(Receipt.chat_id == chat_id, Receipt.status == CONFIRMED, Receipt.transaction_date == day)
        .order_by(Receipt.created_at, Receipt.id)
    )
    return list(result.scalars())


async def get_period_summary(
    session: AsyncSession, chat_id: int, start: date, end: date
) -> PeriodSummary:
    """Confirmed spending between ``start`` and ``end`` (inclusive)."""
    in_period = (
        Receipt.chat_id == chat_id,
        Receipt.status == CONFIRMED,
        Receipt.transaction_date >= start,
        Receipt.transaction_date <= end,
    )
    totals = await session.execute(
        select(func.coalesce(func.sum(Receipt.total), 0), func.count(Receipt.id)).where(*in_period)
    )
    total, count = totals.one()

    # Categories are summed per item (net of receipt discount/tax), since one
    # supermarket receipt can span several categories.
    by_category = await session.execute(
        select(ReceiptItem.category, func.sum(ReceiptItem.net_amount))
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(*in_period)
        .group_by(ReceiptItem.category)
        .order_by(func.sum(ReceiptItem.net_amount).desc())
    )
    return PeriodSummary(
        total=int(total),
        count=int(count),
        by_category=[(category, int(amount)) for category, amount in by_category.all()],
    )


async def get_day_total(session: AsyncSession, chat_id: int, day: date) -> int:
    return (await get_period_summary(session, chat_id, day, day)).total


async def list_receipts_between(
    session: AsyncSession, chat_id: int, start: date, end: date
) -> list[Receipt]:
    """Confirmed receipts in a date range, for /export."""
    result = await session.execute(
        select(Receipt)
        .where(
            Receipt.chat_id == chat_id,
            Receipt.status == CONFIRMED,
            Receipt.transaction_date >= start,
            Receipt.transaction_date <= end,
        )
        .order_by(Receipt.transaction_date, Receipt.id)
    )
    return list(result.scalars())

