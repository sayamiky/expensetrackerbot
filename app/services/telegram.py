"""Thin httpx client for the Telegram Bot API (webhook mode)."""

import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramBotError(RuntimeError):
    """Raised when the Telegram Bot API call fails."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class TelegramClient:
    """Talk to Telegram: send/edit replies, answer button taps, manage the webhook."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # Tests inject an httpx.MockTransport here to avoid real network calls.
        self._transport = transport

    @property
    def _bot_base_url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self._settings.telegram_bot_token}"

    def _check_configured(self) -> None:
        if not self._settings.telegram_bot_token:
            raise TelegramBotError(
                "TELEGRAM_BOT_TOKEN is not configured on the server.",
                status_code=503,
            )

    async def _post(self, method: str, body: dict | None = None, **request_kwargs) -> dict:
        """POST to a Bot API method and return the JSON result.

        ``body`` is sent as JSON; pass ``data``/``files`` instead for multipart.
        """
        self._check_configured()
        url = f"{self._bot_base_url}/{method}"
        if body is not None:
            request_kwargs["json"] = body
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(url, **request_kwargs)
        except httpx.TimeoutException as exc:
            raise TelegramBotError("Telegram request timed out.", 504) from exc
        except httpx.RequestError as exc:
            raise TelegramBotError(f"Could not reach Telegram: {exc}", 502) from exc

        if response.status_code >= 400:
            logger.warning("Telegram %s returned %s: %s", method, response.status_code, response.text)
            raise TelegramBotError(
                f"Telegram {method} failed (HTTP {response.status_code}): {response.text[:200]}",
                502 if response.status_code >= 500 else response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramBotError("Telegram returned a non-JSON response.") from exc

        if not data.get("ok"):
            description = data.get("description", "unknown error")
            raise TelegramBotError(f"Telegram {method} failed: {description}")

        result = data.get("result")
        return result if isinstance(result, dict) else {}

    async def send_message(
        self, chat_id: int | str, text: str, reply_markup: dict | None = None
    ) -> dict:
        """Send a text reply to a chat; returns the sent Message."""
        body: dict = {"chat_id": chat_id, "text": text}
        if reply_markup:
            body["reply_markup"] = reply_markup
        return await self._post("sendMessage", body)

    async def edit_message_text(
        self, chat_id: int | str, message_id: int, text: str, reply_markup: dict | None = None
    ) -> dict:
        """Replace a bot message's text. Omitting reply_markup removes its buttons."""
        body: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            body["reply_markup"] = reply_markup
        return await self._post("editMessageText", body)

    async def edit_message_reply_markup(
        self, chat_id: int | str, message_id: int, reply_markup: dict | None
    ) -> dict:
        """Swap only the buttons; editing the text to the same value is an error."""
        body: dict = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup:
            body["reply_markup"] = reply_markup
        return await self._post("editMessageReplyMarkup", body)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        """Stop the button's loading spinner, optionally with a short toast."""
        body: dict = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        return await self._post("answerCallbackQuery", body)

    async def send_document(
        self, chat_id: int | str, filename: str, content: bytes, caption: str | None = None
    ) -> dict:
        """Upload a file (e.g. the /export CSV) to a chat."""
        data: dict = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        return await self._post(
            "sendDocument",
            data=data,
            files={"document": (filename, content, "text/csv")},
        )

    async def get_file(self, file_id: str) -> str:
        """Return the server-side file path for a file_id."""
        result = await self._post("getFile", {"file_id": file_id})
        file_path = result.get("file_path")
        if not file_path:
            raise TelegramBotError("Telegram getFile returned no file_path.")
        return str(file_path)

    async def download_file(self, file_path: str) -> bytes:
        """Download raw file bytes from the Bot API file endpoint."""
        self._check_configured()
        url = f"{TELEGRAM_API_BASE}/file/bot{self._settings.telegram_bot_token}/{file_path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise TelegramBotError("Telegram file download timed out.", 504) from exc
        except httpx.RequestError as exc:
            raise TelegramBotError(f"Could not reach Telegram: {exc}", 502) from exc

        if response.status_code >= 400:
            raise TelegramBotError(
                f"Telegram file download failed (HTTP {response.status_code}).",
                502 if response.status_code >= 500 else response.status_code,
            )
        return response.content

    async def set_webhook(self, url: str, secret_token: str | None = None) -> dict:
        """Register (or replace) the webhook URL with Telegram.

        Only the update types this bot handles are requested.
        """
        body: dict = {"url": url, "allowed_updates": ["message", "callback_query"]}
        if secret_token:
            body["secret_token"] = secret_token
        return await self._post("setWebhook", body)

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> dict:
        """Register the "/" menu shown in Telegram clients."""
        payload = [{"command": name, "description": description} for name, description in commands]
        return await self._post("setMyCommands", {"commands": payload})

    async def delete_webhook(self) -> dict:
        """Remove the current webhook registration."""
        return await self._post("deleteWebhook", {})
