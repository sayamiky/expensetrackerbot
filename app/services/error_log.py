"""Best-effort error log so /logs can show failures without Vercel's console."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ErrorLog

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 500


async def record_error(
    session: AsyncSession, chat_id: int | None, error_type: str, message: str
) -> None:
    """Persist one error row. Never raises — a logging failure must not mask
    the original error the caller is already handling."""
    try:
        session.add(ErrorLog(chat_id=chat_id, error_type=error_type, message=message[:MAX_MESSAGE_LENGTH]))
        await session.commit()
    except Exception:
        logger.exception("Could not write to error_logs")
        await session.rollback()


async def get_recent_errors(session: AsyncSession, limit: int = 5) -> list[ErrorLog]:
    """Most recent errors across all chats, newest first (by serial id, which
    is strictly increasing even when two timestamps tie)."""
    result = await session.execute(select(ErrorLog).order_by(ErrorLog.id.desc()).limit(limit))
    return list(result.scalars())
