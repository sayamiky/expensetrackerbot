"""Telegram webhook endpoint.

Telegram POSTs every update here:

- A message starting with "/" goes to ``app.services.commands`` (no LLM).
- A photo is downloaded and sent to the LLM as a receipt; the result becomes
  a pending draft with Simpan/Batal buttons (``app.services.expense_flow``).
- Any other text goes to the LLM, which classifies it as an expense, a
  summary question, or chat.
- A tap on an inline button arrives as a callback_query and is handled by
  ``app.services.callbacks``.

The endpoint is protected by the secret token Telegram sends in the
``X-Telegram-Bot-Api-Secret-Token`` header, and always answers 200 so
Telegram does not keep retrying the same update.
"""

import base64
import logging
import time
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import get_rate_limiter
from app.db.session import DatabaseError, get_db_sessionmaker
from app.schemas.telegram import TelegramCallbackQuery, TelegramMessage, TelegramPhotoSize, TelegramUpdate
from app.services import error_log, updates
from app.services.callbacks import handle_callback
from app.services.commands import handle_command
from app.services.expense_flow import handle_analysis
from app.services.formatting import MAX_MESSAGE_CHARS, split_message
from app.services.llm_client import LLMClient, LLMClientError
from app.services.periods import today_local
from app.services.replies import BotReply
from app.services.telegram import TelegramBotError, TelegramClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram"])

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

BUSY_REPLY = "Maaf, layanan sedang sibuk. Coba lagi nanti ya."
READING_RECEIPT = "⏳ Membaca struk…"


def get_llm_client(settings: Settings = Depends(get_settings)) -> LLMClient:
    """Provide an LLM client; overridable in tests."""
    return LLMClient(settings)


def get_telegram_client(settings: Settings = Depends(get_settings)) -> TelegramClient:
    """Provide a Telegram client; overridable in tests."""
    return TelegramClient(settings)


def verify_telegram_secret(
    settings: Settings = Depends(get_settings),
    secret: str | None = Header(default=None, alias=TELEGRAM_SECRET_HEADER),
) -> None:
    """Reject webhook calls that do not carry the secret Telegram configured."""
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured.")
    if not secret or secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")


def _largest_photo(message: TelegramMessage) -> TelegramPhotoSize | None:
    """Return the highest-resolution photo (Telegram sends the largest last)."""
    if not message.photo:
        return None
    return message.photo[-1]


@router.post("/telegram/webhook", summary="Telegram webhook")
async def telegram_webhook(
    payload: TelegramUpdate,
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm_client),
    tg: TelegramClient = Depends(get_telegram_client),
    limiter=Depends(get_rate_limiter),
    db_sessionmaker: Callable[[], AsyncSession] = Depends(get_db_sessionmaker),
    _: None = Depends(verify_telegram_secret),
) -> dict[str, bool]:
    """Handle one Telegram update: a message or an inline-button tap."""
    if payload.callback_query is not None:
        chat_id = _callback_chat_id(payload.callback_query)
    elif payload.message is not None and payload.message.chat is not None:
        chat_id = payload.message.chat.id
    else:
        # Channel post, edited message, etc. — nothing to answer.
        return {"ok": True}

    # Optional allowlist of chat ids.
    allowed = settings.telegram_chat_id_set
    if chat_id is None or (allowed and chat_id not in allowed):
        logger.info("Ignoring update from chat %s (not allowed)", chat_id)
        return {"ok": True}

    # Per-chat throttle, using the in-process limiter.
    if limiter.check(f"telegram:{chat_id}", time.monotonic()) is not None:
        logger.warning("Rate limit hit for telegram chat %s", chat_id)
        return {"ok": True}

    # Filled by _handle_message as soon as the "⏳" message exists, so the
    # error handlers below can overwrite it even when processing fails midway.
    progress: dict[str, int | None] = {"placeholder_id": None}
    try:
        async with db_sessionmaker() as session:
            if not await updates.claim_update(session, payload.update_id):
                logger.info("Skipping duplicate update %s", payload.update_id)
                return {"ok": True}

        if payload.callback_query is not None:
            await _handle_callback_query(db_sessionmaker, settings, tg, chat_id, payload.callback_query)
        else:
            await _handle_message(db_sessionmaker, settings, llm, tg, chat_id, payload.message, progress)
    except LLMClientError as exc:
        logger.warning("LLM failed for chat %s: %s", chat_id, exc)
        await _log_error(db_sessionmaker, chat_id, "LLMClientError", str(exc))
        await _send_busy_reply(tg, chat_id, progress["placeholder_id"])
    except DatabaseError as exc:
        logger.warning("Database unavailable for chat %s: %s", chat_id, exc)
        # Can't write to error_logs when the database is itself the problem.
        await _send_busy_reply(tg, chat_id, progress["placeholder_id"])
    except TelegramBotError as exc:
        logger.warning("Telegram failed for chat %s: %s", chat_id, exc)
        await _log_error(db_sessionmaker, chat_id, "TelegramBotError", str(exc))
    except Exception as exc:
        # Boundary of last resort: an unanticipated bug must still get a reply
        # and a 200 to Telegram, not silence or a 500.
        logger.exception("Unexpected error handling chat %s", chat_id)
        await _log_error(db_sessionmaker, chat_id, type(exc).__name__, str(exc))
        await _send_busy_reply(tg, chat_id, progress["placeholder_id"])

    return {"ok": True}


def _callback_chat_id(callback: TelegramCallbackQuery) -> int | None:
    if callback.message is None or callback.message.chat is None:
        return None
    return callback.message.chat.id


async def _handle_message(
    db_sessionmaker: Callable[[], AsyncSession],
    settings: Settings,
    llm: LLMClient,
    tg: TelegramClient,
    chat_id: int,
    message: TelegramMessage,
    progress: dict[str, int | None],
) -> None:
    """Answer a text or photo message."""
    photo = _largest_photo(message)
    prompt = (message.caption or message.text or "").strip()
    if not prompt and not photo:
        # Sticker or another text-less, photo-less message — nothing to answer.
        return

    if photo is None and prompt.startswith("/"):
        reply = await handle_command(db_sessionmaker, settings, chat_id, prompt)
        await _send_reply(tg, chat_id, reply)
        return

    today = today_local(settings.tz)
    if photo is None:
        analysis, raw = await llm.analyze_expense_message(prompt, today)
        reply = await handle_analysis(db_sessionmaker, settings, chat_id, analysis, raw, source="text")
        await _send_reply(tg, chat_id, reply)
        return

    # Reading a receipt takes several seconds; show progress, then overwrite
    # the same message with the draft.
    sent = await tg.send_message(chat_id, READING_RECEIPT)
    placeholder_id = sent.get("message_id")
    progress["placeholder_id"] = placeholder_id

    file_path = await tg.get_file(photo.file_id)
    raw_image = await tg.download_file(file_path)
    image_url = f"data:image/jpeg;base64,{base64.b64encode(raw_image).decode('ascii')}"

    analysis, raw = await llm.analyze_expense_message(prompt, today, image_url)
    reply = await handle_analysis(
        db_sessionmaker,
        settings,
        chat_id,
        analysis,
        raw,
        source="photo",
        telegram_file_id=photo.file_id,
        note=message.caption,
    )
    if placeholder_id is not None and len(reply.text) <= MAX_MESSAGE_CHARS:
        await tg.edit_message_text(chat_id, placeholder_id, reply.text, reply.reply_markup)
    else:
        await _send_reply(tg, chat_id, reply)


async def _handle_callback_query(
    db_sessionmaker: Callable[[], AsyncSession],
    settings: Settings,
    tg: TelegramClient,
    chat_id: int,
    callback: TelegramCallbackQuery,
) -> None:
    outcome = await handle_callback(db_sessionmaker, settings, chat_id, callback.data or "")
    # Answer first: until then the user's button shows a loading spinner.
    await tg.answer_callback_query(callback.id, outcome.toast)

    message_id = callback.message.message_id if callback.message else None
    if message_id is None:
        return
    if outcome.text is not None:
        await tg.edit_message_text(chat_id, message_id, outcome.text[:MAX_MESSAGE_CHARS], outcome.reply_markup)
    else:
        await tg.edit_message_reply_markup(chat_id, message_id, outcome.reply_markup)


async def _send_reply(tg: TelegramClient, chat_id: int, reply: BotReply) -> None:
    """Send a reply, split across messages if long; buttons go on the last one."""
    chunks = split_message(reply.text)
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        await tg.send_message(chat_id, chunk, reply.reply_markup if is_last else None)
    if reply.document is not None:
        filename, content = reply.document
        await tg.send_document(chat_id, filename, content)


async def _send_busy_reply(tg: TelegramClient, chat_id: int, placeholder_id: int | None = None) -> None:
    """Best-effort fallback message; failures here are only logged."""
    try:
        if placeholder_id is not None:
            await tg.edit_message_text(chat_id, placeholder_id, BUSY_REPLY)
        else:
            await tg.send_message(chat_id, BUSY_REPLY)
    except TelegramBotError:
        logger.exception("Could not send error reply to chat %s", chat_id)


async def _log_error(
    db_sessionmaker: Callable[[], AsyncSession], chat_id: int, error_type: str, message: str
) -> None:
    """Best-effort write to error_logs (for /logs); never raises."""
    try:
        async with db_sessionmaker() as session:
            await error_log.record_error(session, chat_id, error_type, message)
    except Exception:
        logger.exception("Could not record error_log entry for chat %s", chat_id)
