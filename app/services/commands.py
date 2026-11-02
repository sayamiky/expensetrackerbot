"""Slash commands. These bypass the LLM entirely: parsing and validation
happen here, against whatever the user typed after the command name."""

import csv
import io
import re
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.schemas.expense import CATEGORY_LABELS, category_label, parse_category
from app.services import budget, error_log, expense_log
from app.services.formatting import format_history, format_summary, rupiah, to_local
from app.services.periods import MONTHS_ID, month_range, period_range, today_local
from app.services.replies import BotReply

# Registered with Telegram's setMyCommands (scripts/set_webhook.py), so they
# show up in the "/" menu.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("history", "Semua transaksi hari ini"),
    ("today", "Total hari ini per kategori"),
    ("week", "Total 7 hari terakhir per kategori"),
    ("month", "Total bulan ini per kategori + budget"),
    ("categories", "Daftar kategori"),
    ("category", "Ubah kategori: /category <id> <kategori>"),
    ("delete", "Hapus transaksi: /delete <id>"),
    ("budget", "Lihat/set budget: /budget <kategori> <nominal>"),
    ("export", "Kirim CSV transaksi bulan ini"),
    ("help", "Cara pakai bot"),
]

HELP_TEXT = (
    "💸 Bot pencatat pengeluaran\n\n"
    "Cara mencatat:\n"
    "• Kirim foto struk belanja → cek draft → tekan ✅ Simpan\n"
    "• Atau tulis saja, misalnya: makan siang 25rb, bensin 50k kemarin\n\n"
    "Perintah:\n"
    "/history — semua transaksi hari ini\n"
    "/history yesterday atau /history 2026-11-01 — tanggal lain\n"
    "/today, /week, /month — ringkasan per kategori\n"
    "/categories — daftar kategori\n"
    "/category <id> <kategori> — ubah kategori, contoh: /category 42 food\n"
    "/delete <id> — hapus transaksi, contoh: /delete 42\n"
    "/budget — lihat budget bulan ini\n"
    "/budget <kategori> <nominal> — set budget, contoh: /budget food 2jt (0 = hapus)\n"
    "/export — CSV transaksi bulan ini (/export 2026-10 untuk bulan lain)\n"
    "/logs — error terakhir (buat debug)"
)

HISTORY_USAGE = "Format: /history [yesterday | YYYY-MM-DD]\nContoh: /history 2026-11-01"
CATEGORY_USAGE = "Format: /category <id> <kategori>\nContoh: /category 42 food\nLihat /categories untuk daftar kategori."
DELETE_USAGE = "Format: /delete <id>\nContoh: /delete 42\nNomor id ada di /history."
BUDGET_USAGE = "Format: /budget <kategori> <nominal>\nContoh: /budget food 2000000 atau /budget food 2jt\nNominal 0 menghapus budget."
EXPORT_USAGE = "Format: /export [YYYY-MM]\nContoh: /export 2026-10"

_AMOUNT_RE = re.compile(r"^(\d+(?:[.,]\d+)*)(rb|ribu|k|jt|juta)?$")
_MULTIPLIERS = {"rb": 1_000, "ribu": 1_000, "k": 1_000, "jt": 1_000_000, "juta": 1_000_000}


def parse_command(text: str) -> tuple[str, list[str]]:
    """Split "/cmd@BotName arg1 arg2" into ("/cmd", ["arg1", "arg2"])."""
    parts = text.strip().split()
    command = parts[0].split("@")[0].lower()
    return command, parts[1:]


def parse_amount(raw: str) -> int | None:
    """"2000000", "2.000.000", "2jt", "1,5jt", "500rb", "Rp50k" -> rupiah."""
    cleaned = raw.strip().lower().replace("rp", "").replace(" ", "")
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        return None
    number, suffix = match.groups()
    if suffix:
        # With a suffix, a single separator is a decimal point: "1,5jt".
        try:
            value = float(number.replace(",", "."))
        except ValueError:
            return None
        return round(value * _MULTIPLIERS[suffix])
    return int(number.replace(".", "").replace(",", ""))


async def handle_command(
    db_sessionmaker: Callable[[], AsyncSession], settings: Settings, chat_id: int, text: str
) -> BotReply:
    """Route a "/..." message to its handler; unknown commands get the help text."""
    command, args = parse_command(text)
    today = today_local(settings.tz)

    if command in ("/start", "/help"):
        return BotReply(HELP_TEXT)
    if command == "/history":
        return await _handle_history(db_sessionmaker, settings, chat_id, args, today)
    if command in ("/today", "/week", "/month"):
        return await _handle_summary(db_sessionmaker, chat_id, command[1:], today)
    if command == "/categories":
        return BotReply(_categories_text())
    if command == "/category":
        return await _handle_category(db_sessionmaker, chat_id, args)
    if command == "/delete":
        return await _handle_delete(db_sessionmaker, chat_id, args)
    if command == "/budget":
        return await _handle_budget(db_sessionmaker, chat_id, args, today)
    if command == "/export":
        return await _handle_export(db_sessionmaker, chat_id, args, today)
    if command == "/logs":
        return await _handle_logs(db_sessionmaker, settings)
    return BotReply("Perintah tidak dikenal.\n\n" + HELP_TEXT)


async def _handle_history(
    db_sessionmaker: Callable[[], AsyncSession], settings: Settings, chat_id: int, args: list[str], today: date
) -> BotReply:
    if not args:
        day = today
    elif len(args) == 1 and args[0].lower() in ("yesterday", "kemarin"):
        day = today - timedelta(days=1)
    elif len(args) == 1:
        try:
            day = date.fromisoformat(args[0])
        except ValueError:
            return BotReply(HISTORY_USAGE)
    else:
        return BotReply(HISTORY_USAGE)

    async with db_sessionmaker() as session:
        receipts = await expense_log.list_receipts_for_day(session, chat_id, day)
    return BotReply(format_history(receipts, day, settings.tz))


async def _handle_summary(
    db_sessionmaker: Callable[[], AsyncSession], chat_id: int, period: str, today: date
) -> BotReply:
    start, end, label = period_range(period, today)
    async with db_sessionmaker() as session:
        summary = await expense_log.get_period_summary(session, chat_id, start, end)
        budgets = await budget.get_budgets(session, chat_id) if period == "month" else {}
    return BotReply(format_summary(summary, label, budgets))


def _categories_text() -> str:
    lines = ["Daftar kategori (pakai nama di kiri untuk perintah):"]
    lines.extend(f"• {category.value} — {label}" for category, label in CATEGORY_LABELS.items())
    return "\n".join(lines)


async def _handle_category(
    db_sessionmaker: Callable[[], AsyncSession], chat_id: int, args: list[str]
) -> BotReply:
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        return BotReply(CATEGORY_USAGE)
    category = parse_category(" ".join(args[1:]))
    if category is None:
        return BotReply("Kategori tidak dikenal.\n\n" + _categories_text())

    async with db_sessionmaker() as session:
        receipt = await expense_log.set_category(session, chat_id, int(args[0].lstrip("#")), category)
    if receipt is None:
        return BotReply("Transaksi tidak ditemukan. Cek nomornya di /history.")
    return BotReply(f"Kategori transaksi #{receipt.id} diubah ke {category_label(category.value)}.")


async def _handle_delete(
    db_sessionmaker: Callable[[], AsyncSession], chat_id: int, args: list[str]
) -> BotReply:
    if len(args) != 1 or not args[0].lstrip("#").isdigit():
        return BotReply(DELETE_USAGE)
    receipt_id = int(args[0].lstrip("#"))
    async with db_sessionmaker() as session:
        deleted = await expense_log.delete_receipt(session, chat_id, receipt_id)
    if not deleted:
        return BotReply("Transaksi tidak ditemukan. Cek nomornya di /history.")
    return BotReply(f"🗑️ Transaksi #{receipt_id} dihapus.")


async def _handle_budget(
    db_sessionmaker: Callable[[], AsyncSession], chat_id: int, args: list[str], today: date
) -> BotReply:
    if not args:
        start, end = month_range(today)
        async with db_sessionmaker() as session:
            budgets = await budget.get_budgets(session, chat_id)
            summary = await expense_log.get_period_summary(session, chat_id, start, end)
        if not budgets:
            return BotReply("Belum ada budget.\n\n" + BUDGET_USAGE)
        spent = dict(summary.by_category)
        lines = [f"💰 Budget {MONTHS_ID[today.month - 1]} {today.year}:"]
        for category, limit in sorted(budgets.items()):
            amount = spent.get(category, 0)
            lines.append(
                f"• {category_label(category)}: {rupiah(amount)} / {rupiah(limit)} ({amount * 100 // limit}%)"
            )
        return BotReply("\n".join(lines))

    if len(args) != 2:
        return BotReply(BUDGET_USAGE)
    category = parse_category(args[0])
    amount = parse_amount(args[1])
    if category is None:
        return BotReply("Kategori tidak dikenal.\n\n" + _categories_text())
    if amount is None:
        return BotReply("Nominal tidak valid.\n\n" + BUDGET_USAGE)

    async with db_sessionmaker() as session:
        await budget.set_budget(session, chat_id, category, amount)
    label = category_label(category.value)
    if amount == 0:
        return BotReply(f"Budget {label} dihapus.")
    return BotReply(f"Budget {label} diset {rupiah(amount)} per bulan.")


async def _handle_export(
    db_sessionmaker: Callable[[], AsyncSession], chat_id: int, args: list[str], today: date
) -> BotReply:
    if not args:
        month_day = today
    elif len(args) == 1:
        try:
            month_day = date.fromisoformat(f"{args[0]}-01")
        except ValueError:
            return BotReply(EXPORT_USAGE)
    else:
        return BotReply(EXPORT_USAGE)

    start, end = month_range(month_day)
    async with db_sessionmaker() as session:
        receipts = await expense_log.list_receipts_between(session, chat_id, start, end)

    month_label = f"{MONTHS_ID[start.month - 1]} {start.year}"
    if not receipts:
        return BotReply(f"Belum ada transaksi di {month_label}.")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["date", "receipt_id", "merchant", "item", "qty", "unit_price", "item_total",
         "net_amount", "category", "payment_method", "source"]
    )
    for receipt in receipts:
        for item in receipt.items:
            writer.writerow([
                receipt.transaction_date.isoformat(), receipt.id, receipt.merchant or "", item.name,
                f"{item.qty:g}", item.unit_price if item.unit_price is not None else "", item.total,
                item.net_amount, item.category, receipt.payment_method or "", receipt.source,
            ])

    total = sum(receipt.total for receipt in receipts)
    # utf-8-sig so Excel opens item names with accents/emoji correctly.
    content = buffer.getvalue().encode("utf-8-sig")
    return BotReply(
        f"📎 Export {month_label}: {len(receipts)} transaksi, total {rupiah(total)}.",
        document=(f"expenses-{start:%Y-%m}.csv", content),
    )


async def _handle_logs(db_sessionmaker: Callable[[], AsyncSession], settings: Settings) -> BotReply:
    async with db_sessionmaker() as session:
        errors = await error_log.get_recent_errors(session, limit=5)

    if not errors:
        return BotReply("Belum ada error yang tercatat.")

    lines = ["5 error terakhir:"]
    for entry in errors:
        local_time = to_local(entry.created_at, settings.tz)
        lines.append(
            f"- [{local_time:%d/%m %H:%M} WITA] {entry.error_type} (chat {entry.chat_id}): {entry.message}"
        )
    return BotReply("\n".join(lines))
