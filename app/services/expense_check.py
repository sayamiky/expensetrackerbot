"""Sanity checks on what the LLM extracted, before anything is saved.

The model reads the receipt; this module decides whether the numbers add up.
Small inconsistencies become warnings shown on the draft (the user can still
save it); a missing amount or an unreadable photo is rejected outright.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.schemas.expense import Category, ExpenseAnalysis, ExpenseItem
from app.services.formatting import rupiah


class ExpenseRejected(Exception):
    """The analysis cannot be saved; ``str(exc)`` is the reply for the user."""


@dataclass
class CheckedExpense:
    """A validated expense, ready to be stored."""

    merchant: str | None
    transaction_date: date
    items: list[ExpenseItem]
    # Each item's share of ``total`` after receipt-level discount/tax.
    net_amounts: list[int]
    subtotal: int
    discount: int
    tax: int
    total: int
    payment_method: str | None
    category: Category
    warnings: list[str] = field(default_factory=list)


def _tolerance(amount: int) -> int:
    """Rounding slack: Rp 100 or 1%, whichever is larger."""
    return max(100, round(amount * 0.01))


def check_expense(
    analysis: ExpenseAnalysis, today: date, source: str, min_confidence: float
) -> CheckedExpense:
    """Validate an "expense_log" analysis. Raises ExpenseRejected."""
    if source == "photo":
        if not analysis.is_receipt:
            raise ExpenseRejected(
                "Sepertinya ini bukan foto struk belanja. Kirim foto struk, atau tulis "
                "pengeluarannya, misalnya: makan siang 25rb"
            )
        if analysis.confidence < min_confidence:
            raise ExpenseRejected(
                "Struknya kurang jelas terbaca. Coba foto ulang dengan cahaya cukup, "
                "struk tidak terlipat, dan seluruh bagian total terlihat ya."
            )

    items = [item for item in analysis.items if item.total > 0]
    if not items:
        raise ExpenseRejected(
            "Aku belum menemukan nominal pengeluarannya. Coba tulis lengkap, "
            "misalnya: bensin 50rb"
        )

    warnings: list[str] = []
    items_sum = sum(item.total for item in items)

    subtotal = analysis.subtotal if analysis.subtotal is not None else items_sum
    if analysis.subtotal is not None and abs(subtotal - items_sum) > _tolerance(subtotal):
        warnings.append(
            f"Jumlah semua item ({rupiah(items_sum)}) tidak sama dengan subtotal struk ({rupiah(subtotal)})."
        )

    expected_total = subtotal - analysis.discount + analysis.tax
    total = analysis.total if analysis.total is not None else expected_total
    if analysis.total is not None and abs(total - expected_total) > _tolerance(total):
        warnings.append(
            f"Subtotal − diskon + pajak ({rupiah(expected_total)}) tidak sama dengan total struk ({rupiah(total)})."
        )
    if total <= 0:
        raise ExpenseRejected("Total pengeluarannya terbaca Rp 0. Coba kirim ulang ya.")

    transaction_date = analysis.transaction_date or today
    if transaction_date > today:
        warnings.append("Tanggal di struk ada di masa depan, jadi aku pakai tanggal hari ini.")
        transaction_date = today
    elif transaction_date < today - timedelta(days=365):
        warnings.append(f"Tanggal transaksi {transaction_date:%d/%m/%Y} sudah lebih dari setahun lalu.")

    net_amounts = allocate([item.total for item in items], total)

    return CheckedExpense(
        merchant=analysis.merchant,
        transaction_date=transaction_date,
        items=items,
        net_amounts=net_amounts,
        subtotal=subtotal,
        discount=analysis.discount,
        tax=analysis.tax,
        total=total,
        payment_method=analysis.payment_method,
        category=dominant_category(items, net_amounts),
        warnings=warnings,
    )


def allocate(weights: list[int], total: int) -> list[int]:
    """Split ``total`` proportionally to ``weights`` into whole rupiah that sum
    exactly to ``total`` (largest-remainder method)."""
    if not weights:
        return []
    base = sum(weights)
    if base <= 0:
        weights, base = [1] * len(weights), len(weights)

    raw = [total * weight / base for weight in weights]
    shares = [math.floor(value) for value in raw]
    remainder = total - sum(shares)
    by_fraction = sorted(range(len(raw)), key=lambda i: raw[i] - shares[i], reverse=True)
    for index in by_fraction[:remainder]:
        shares[index] += 1
    return shares


def dominant_category(items: list[ExpenseItem], net_amounts: list[int]) -> Category:
    """The category with the largest share of the receipt."""
    totals: dict[Category, int] = {}
    for item, amount in zip(items, net_amounts):
        totals[item.category] = totals.get(item.category, 0) + amount
    return max(totals, key=lambda category: totals[category])
