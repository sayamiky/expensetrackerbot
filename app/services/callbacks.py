"""Inline-button taps: save/discard drafts, undo, and change a category."""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Receipt
from app.schemas.expense import category_label, parse_category
from app.services import budget, expense_log
from app.services.formatting import format_draft, format_receipt, format_saved_text_expense, rupiah
from app.services.keyboards import category_keyboard, draft_keyboard, parse_callback, saved_keyboard
from app.services.periods import today_local
from app.services.replies import CallbackOutcome

NOT_FOUND = "Transaksi tidak ditemukan (mungkin sudah dihapus)."


async def handle_callback(
    db_sessionmaker: Callable[[], AsyncSession], settings: Settings, chat_id: int, data: str
) -> CallbackOutcome:
    parsed = parse_callback(data)
    if parsed is None:
        return CallbackOutcome(toast="Tombol tidak dikenal.")
    action, receipt_id, argument = parsed
    today = today_local(settings.tz)

    async with db_sessionmaker() as session:
        if action == "cancel":
            receipt = await expense_log.get_receipt(session, chat_id, receipt_id)
            if receipt is None:
                return CallbackOutcome(toast=NOT_FOUND)
            was_pending = receipt.status == expense_log.PENDING
            await expense_log.delete_receipt(session, chat_id, receipt_id)
            text = (
                f"❌ Draft #{receipt_id} dibatalkan, tidak disimpan."
                if was_pending
                else f"↩️ Transaksi #{receipt_id} ({rupiah(receipt.total)}) dibatalkan dan dihapus."
            )
            return CallbackOutcome(toast="Dibatalkan", text=text)

        receipt = await expense_log.get_receipt(session, chat_id, receipt_id)
        if receipt is None:
            return CallbackOutcome(toast=NOT_FOUND)

        if action == "confirm":
            receipt = await expense_log.confirm_receipt(session, chat_id, receipt_id)
            text, markup = await _render(session, settings, receipt, today)
            warnings = await budget.budget_warnings(
                session, chat_id, {item.category for item in receipt.items}, today, settings.budget_warning_ratio
            )
            if warnings:
                text += "\n\n" + "\n".join(warnings)
            return CallbackOutcome(toast="Tersimpan ✅", text=text, reply_markup=markup)

        if action == "editcat":
            return CallbackOutcome(reply_markup=category_keyboard(receipt_id))

        if action == "back":
            return CallbackOutcome(reply_markup=_keyboard_for(receipt))

        if action == "setcat":
            category = parse_category(argument or "")
            if category is None:
                return CallbackOutcome(toast="Kategori tidak dikenal.")
            receipt = await expense_log.set_category(session, chat_id, receipt_id, category)
            text, markup = await _render(session, settings, receipt, today)
            return CallbackOutcome(toast=f"Kategori: {category_label(category.value)}", text=text, reply_markup=markup)

    return CallbackOutcome(toast="Tombol tidak dikenal.")


def _keyboard_for(receipt: Receipt) -> dict:
    return draft_keyboard(receipt.id) if receipt.status == expense_log.PENDING else saved_keyboard(receipt.id)


async def _render(session: AsyncSession, settings: Settings, receipt: Receipt, today) -> tuple[str, dict]:
    """The message text and buttons for a receipt in its current state."""
    if receipt.status == expense_log.PENDING:
        return format_draft(receipt), draft_keyboard(receipt.id)

    day_total = await expense_log.get_day_total(session, receipt.chat_id, today)
    if receipt.source == "text":
        return format_saved_text_expense(receipt, day_total), saved_keyboard(receipt.id)
    text = format_receipt(receipt, f"✅ Struk tersimpan #{receipt.id}") + f"\nTotal hari ini: {rupiah(day_total)}"
    return text, saved_keyboard(receipt.id)
