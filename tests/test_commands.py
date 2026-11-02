"""Tests for slash commands (no LLM involved)."""

import asyncio
from datetime import date, datetime, timezone

from app.core.config import Settings
from app.schemas.expense import ExpenseAnalysis
from app.services import expense_log
from app.services.commands import handle_command, parse_amount
from app.services.expense_check import check_expense
from app.services.periods import today_local

SETTINGS = Settings(database_url="", timezone="Asia/Makassar")
CHAT = 7


def _seed(db_sessionmaker, name: str, total: int, category: str, day: date, status: str = "confirmed") -> int:
    analysis = ExpenseAnalysis.model_validate(
        {"intent": "expense_log", "items": [{"name": name, "total": total, "category": category}]}
    )
    checked = check_expense(analysis, day, "text", 0.5)

    async def run() -> int:
        async with db_sessionmaker() as session:
            receipt = await expense_log.create_receipt(session, CHAT, checked, source="text", status=status)
            return receipt.id

    return asyncio.run(run())


def run_command(db_sessionmaker, text: str):
    return asyncio.run(handle_command(db_sessionmaker, SETTINGS, CHAT, text))


def test_today_local_uses_wita():
    # 16:30 UTC on Nov 1 is already 00:30 on Nov 2 in WITA (UTC+8).
    now = datetime(2026, 11, 1, 16, 30, tzinfo=timezone.utc)
    assert today_local(SETTINGS.tz, now) == date(2026, 11, 2)


def test_parse_amount_variants():
    assert parse_amount("2000000") == 2_000_000
    assert parse_amount("2.000.000") == 2_000_000
    assert parse_amount("2jt") == 2_000_000
    assert parse_amount("1,5jt") == 1_500_000
    assert parse_amount("500rb") == 500_000
    assert parse_amount("Rp50k") == 50_000
    assert parse_amount("abc") is None


def test_history_lists_only_todays_confirmed_transactions(db_sessionmaker):
    today = today_local(SETTINGS.tz)
    first = _seed(db_sessionmaker, "Bensin", 50000, "transportation", today)
    second = _seed(db_sessionmaker, "Makan siang", 25000, "food", today)
    _seed(db_sessionmaker, "Draft", 99000, "other", today, status="pending")
    _seed(db_sessionmaker, "Kemarin", 10000, "snack", date(2020, 1, 1))

    text = run_command(db_sessionmaker, "/history").text
    assert f"#{first}" in text and f"#{second}" in text
    assert "Draft" not in text and "Kemarin" not in text
    assert "Total: Rp 75.000 (2 transaksi)" in text
    assert text.index("Bensin") < text.index("Makan siang")


def test_history_for_specific_date(db_sessionmaker):
    _seed(db_sessionmaker, "Kopi", 18000, "food", date(2026, 10, 30))
    text = run_command(db_sessionmaker, "/history 2026-10-30").text
    assert "Kopi" in text
    assert "Belum ada transaksi" in run_command(db_sessionmaker, "/history 2026-10-29").text
    assert "Format" in run_command(db_sessionmaker, "/history besok-lusa").text


def test_month_summary_shows_budget_progress(db_sessionmaker):
    today = today_local(SETTINGS.tz)
    _seed(db_sessionmaker, "Makan", 160000, "food", today)
    assert "diset Rp 200.000" in run_command(db_sessionmaker, "/budget food 200rb").text

    text = run_command(db_sessionmaker, "/month").text
    assert "Food: Rp 160.000 / Rp 200.000 (80%)" in text
    assert "Food: Rp 160.000 / Rp 200.000 (80%)" in run_command(db_sessionmaker, "/budget").text


def test_category_and_delete_commands(db_sessionmaker):
    receipt_id = _seed(db_sessionmaker, "Netflix", 65000, "entertainment", today_local(SETTINGS.tz))

    assert "Subscription" in run_command(db_sessionmaker, f"/category {receipt_id} subscription").text
    assert "Kategori tidak dikenal" in run_command(db_sessionmaker, f"/category {receipt_id} groceries").text
    assert "dihapus" in run_command(db_sessionmaker, f"/delete {receipt_id}").text
    assert "tidak ditemukan" in run_command(db_sessionmaker, f"/delete {receipt_id}").text


def test_export_returns_csv(db_sessionmaker):
    today = today_local(SETTINGS.tz)
    _seed(db_sessionmaker, "Sabun", 12000, "house", today)
    reply = run_command(db_sessionmaker, "/export")
    assert reply.document is not None
    filename, content = reply.document
    assert filename == f"expenses-{today:%Y-%m}.csv"
    assert "Sabun" in content.decode("utf-8-sig")


def test_unknown_command_shows_help(db_sessionmaker):
    assert "Perintah tidak dikenal" in run_command(db_sessionmaker, "/foo").text
