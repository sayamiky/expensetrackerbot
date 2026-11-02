"""Monthly per-category budgets and the warnings shown when nearing them."""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Budget
from app.schemas.expense import Category, category_label
from app.services import expense_log
from app.services.formatting import rupiah
from app.services.periods import month_range


async def set_budget(session: AsyncSession, chat_id: int, category: Category, monthly_limit: int) -> None:
    """Create, replace, or (with a limit of 0) remove a category's budget."""
    existing = await session.get(Budget, (chat_id, category.value))
    if monthly_limit <= 0:
        if existing is not None:
            await session.delete(existing)
    elif existing is None:
        session.add(Budget(chat_id=chat_id, category=category.value, monthly_limit=monthly_limit))
    else:
        existing.monthly_limit = monthly_limit
        existing.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def get_budgets(session: AsyncSession, chat_id: int) -> dict[str, int]:
    """category slug -> monthly limit."""
    result = await session.execute(select(Budget).where(Budget.chat_id == chat_id))
    return {budget.category: budget.monthly_limit for budget in result.scalars()}


async def budget_warnings(
    session: AsyncSession, chat_id: int, categories: set[str], today: date, ratio: float
) -> list[str]:
    """Warnings for the given categories whose month-to-date spend reached ``ratio``."""
    budgets = await get_budgets(session, chat_id)
    relevant = {category: budgets[category] for category in categories if category in budgets}
    if not relevant:
        return []

    start, end = month_range(today)
    summary = await expense_log.get_period_summary(session, chat_id, start, end)
    spent = dict(summary.by_category)

    warnings: list[str] = []
    for category, limit in sorted(relevant.items()):
        amount = spent.get(category, 0)
        label = category_label(category)
        if amount >= limit:
            warnings.append(
                f"🚨 Budget {label} bulan ini terlampaui: {rupiah(amount)} dari {rupiah(limit)}."
            )
        elif amount >= limit * ratio:
            warnings.append(
                f"⚠️ Budget {label} bulan ini sudah {amount * 100 // limit}%: {rupiah(amount)} dari {rupiah(limit)}."
            )
    return warnings
