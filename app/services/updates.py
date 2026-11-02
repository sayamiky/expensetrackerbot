"""Deduplicate Telegram updates (Telegram re-sends when the webhook is slow)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedUpdate


async def claim_update(session: AsyncSession, update_id: int) -> bool:
    """Record ``update_id``; False when it was already handled.

    Relies on the primary key, so two concurrent deliveries of the same
    update cannot both win.
    """
    session.add(ProcessedUpdate(update_id=update_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True
