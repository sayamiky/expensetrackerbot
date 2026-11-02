"""Turn the LLM's analysis of a message into a saved expense and a reply."""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.schemas.expense import ExpenseAnalysis
from app.services import budget, expense_log
from app.services.expense_check import ExpenseRejected, check_expense
from app.services.formatting import format_draft, format_saved_text_expense, format_summary
from app.services.keyboards import draft_keyboard, saved_keyboard
from app.services.periods import period_range, today_local
from app.services.replies import BotReply

CHAT_FALLBACK = (
    "Kirim foto struk belanja, atau tulis pengeluaranmu, misalnya:\n"
    "• makan siang 25rb\n"
    "• bensin 50k kemarin\n\n"
    "Ketik /help untuk daftar perintah."
)


async def handle_analysis(
    db_sessionmaker: Callable[[], AsyncSession],
    settings: Settings,
    chat_id: int,
    analysis: ExpenseAnalysis,
    raw_llm_json: dict,
    *,
    source: str,
    telegram_file_id: str | None = None,
    note: str | None = None,
) -> BotReply:
    """Save what needs saving and build the reply.

    A photo becomes a pending draft with Simpan/Batal buttons; a typed
    expense is saved immediately with an undo button. For "summary_query"
    the numbers come from the database, never from the model.
    """
    today = today_local(settings.tz)

    if analysis.intent == "summary_query":
        start, end, label = period_range(analysis.summary_period or "today", today)
        async with db_sessionmaker() as session:
            summary = await expense_log.get_period_summary(session, chat_id, start, end)
            budgets = await budget.get_budgets(session, chat_id) if analysis.summary_period == "month" else {}
        return BotReply(format_summary(summary, label, budgets))

    if analysis.intent != "expense_log":
        return BotReply(analysis.reply.strip() or CHAT_FALLBACK)

    try:
        checked = check_expense(analysis, today, source, settings.min_receipt_confidence)
    except ExpenseRejected as exc:
        return BotReply(str(exc))

    is_photo = source == "photo"
    async with db_sessionmaker() as session:
        receipt = await expense_log.create_receipt(
            session,
            chat_id,
            checked,
            source=source,
            status=expense_log.PENDING if is_photo else expense_log.CONFIRMED,
            telegram_file_id=telegram_file_id,
            raw_llm_json=raw_llm_json,
            confidence=analysis.confidence,
            note=note,
        )
        if is_photo:
            return BotReply(format_draft(receipt, checked.warnings), draft_keyboard(receipt.id))

        day_total = await expense_log.get_day_total(session, chat_id, today)
        warnings = await budget.budget_warnings(
            session, chat_id, {item.category for item in receipt.items}, today, settings.budget_warning_ratio
        )

    text = format_saved_text_expense(receipt, day_total)
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    return BotReply(text, saved_keyboard(receipt.id))
