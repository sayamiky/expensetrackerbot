"""Local-time date helpers: what "today", "this week" and "this month" mean."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MONTHS_ID = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]
MONTHS_ID_SHORT = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
DAYS_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


def today_local(tz: ZoneInfo, now: datetime | None = None) -> date:
    """Today's date in the bot's time zone (WITA by default), not UTC's."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(tz).date()


def period_range(period: str, today: date) -> tuple[date, date, str]:
    """Inclusive (start, end) dates and an Indonesian label for a summary period."""
    if period == "week":
        return today - timedelta(days=6), today, "7 hari terakhir"
    if period == "month":
        return month_range(today) + (f"Bulan ini ({MONTHS_ID[today.month - 1]} {today.year})",)
    return today, today, "Hari ini"


def month_range(day: date) -> tuple[date, date]:
    """First and last day of ``day``'s calendar month."""
    start = day.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


def format_long_date(day: date) -> str:
    """e.g. "Senin, 2 Nov 2026"."""
    return f"{DAYS_ID[day.weekday()]}, {day.day} {MONTHS_ID_SHORT[day.month - 1]} {day.year}"
