"""Turn database rows into the Indonesian text the bot sends.

Replies are plain text (no parse_mode), so merchant or item names never need
Markdown/HTML escaping.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.schemas.expense import category_label
from app.services.periods import format_long_date

# Telegram rejects messages over 4096 characters; leave some headroom.
MAX_MESSAGE_CHARS = 4000


def rupiah(amount: int) -> str:
    """12500 -> "Rp 12.500"."""
    return "Rp " + f"{amount:,}".replace(",", ".")


def to_local(moment: datetime, tz: ZoneInfo) -> datetime:
    """Convert a stored timestamp to local time. Timestamps are written in
    UTC; a driver that drops the offset (SQLite) hands them back naive."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz)


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so each chunk fits in one Telegram message."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line[:limit]
        else:
            current = candidate[:limit]
    if current or not chunks:
        chunks.append(current)
    return chunks


def format_receipt(receipt, heading: str, warnings: list[str] | None = None) -> str:
    """A receipt with its items; used for drafts and saved photo receipts."""
    lines = [heading, ""]
    if receipt.merchant:
        lines.append(f"🏪 {receipt.merchant}")
    lines.append(f"📅 {format_long_date(receipt.transaction_date)}")
    if receipt.payment_method:
        lines.append(f"💳 {receipt.payment_method}")
    lines.append("")

    for item in receipt.items:
        qty = f"{item.qty:g}x " if item.qty != 1 else ""
        lines.append(f"• {qty}{item.name} — {rupiah(item.total)} [{category_label(item.category)}]")

    lines.append("")
    if receipt.discount or receipt.tax:
        lines.append(f"Subtotal: {rupiah(receipt.subtotal)}")
        if receipt.discount:
            lines.append(f"Diskon: −{rupiah(receipt.discount)}")
        if receipt.tax:
            lines.append(f"Pajak/biaya: {rupiah(receipt.tax)}")
    lines.append(f"Total: {rupiah(receipt.total)}")

    if warnings:
        lines.append("")
        lines.extend(f"⚠️ {warning}" for warning in warnings)
    return "\n".join(lines)


def format_draft(receipt, warnings: list[str] | None = None) -> str:
    return format_receipt(
        receipt, f"🧾 Struk terbaca (draft #{receipt.id}). Cek dulu sebelum disimpan:", warnings
    ) + "\n\nTekan ✅ Simpan kalau sudah benar."


def format_saved_text_expense(receipt, day_total: int) -> str:
    """Short confirmation for a typed expense (one or a few items)."""
    if len(receipt.items) == 1:
        item = receipt.items[0]
        body = f"✅ Tercatat #{receipt.id}: {item.name} — {rupiah(receipt.total)} ({category_label(item.category)})"
    else:
        lines = [f"✅ Tercatat #{receipt.id}:"]
        lines.extend(
            f"• {item.name} — {rupiah(item.net_amount)} ({category_label(item.category)})"
            for item in receipt.items
        )
        lines.append(f"Total: {rupiah(receipt.total)}")
        body = "\n".join(lines)
    return f"{body}\n📅 {format_long_date(receipt.transaction_date)}\nTotal hari ini: {rupiah(day_total)}"


def format_history(receipts: list, day: date, tz: ZoneInfo) -> str:
    """/history: every confirmed transaction on ``day``, oldest first."""
    heading = f"📋 Transaksi — {format_long_date(day)}"
    if not receipts:
        return f"{heading}\n\nBelum ada transaksi di tanggal ini."

    lines = [heading, ""]
    for receipt in receipts:
        time = to_local(receipt.created_at, tz).strftime("%H:%M")
        if receipt.merchant:
            description = receipt.merchant
        elif len(receipt.items) == 1:
            description = receipt.items[0].name
        else:
            description = f"{len(receipt.items)} item"
        if receipt.merchant and len(receipt.items) > 1:
            description += f" ({len(receipt.items)} item)"
        lines.append(
            f"#{receipt.id}  {time}  {category_label(receipt.category)} — {description} — {rupiah(receipt.total)}"
        )

    total = sum(receipt.total for receipt in receipts)
    lines.append("")
    lines.append(f"Total: {rupiah(total)} ({len(receipts)} transaksi)")
    return "\n".join(lines)


def format_summary(summary, label: str, budgets: dict[str, int] | None = None) -> str:
    """/today, /week, /month: total plus a per-category breakdown."""
    if summary.count == 0:
        return f"📊 {label}\n\nBelum ada pengeluaran."

    lines = [f"📊 {label}", f"Total: {rupiah(summary.total)} ({summary.count} transaksi)", ""]
    for category, amount in summary.by_category:
        line = f"• {category_label(category)}: {rupiah(amount)}"
        limit = (budgets or {}).get(category)
        if limit:
            line += f" / {rupiah(limit)} ({amount * 100 // limit}%)"
        lines.append(line)
    return "\n".join(lines)
