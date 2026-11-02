import json
import logging
from datetime import date

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.expense import CATEGORY_LABELS, Category, ExpenseAnalysis

logger = logging.getLogger(__name__)

_CATEGORY_GUIDE = "\n".join(
    f'- "{category.value}" ({label})' for category, label in CATEGORY_LABELS.items()
)

EXPENSE_SYSTEM_PROMPT = (
    "You are the assistant of an expense-tracking Telegram bot for an "
    "Indonesian user. Today's date is {today} ({timezone}). Look at the "
    "user's message and, if present, an attached photo. Decide exactly one "
    "intent:\n"
    '- "expense_log": the user reports money they spent — a photo of a '
    "shopping receipt/struk/invoice, or a text such as \"makan siang 25rb\" or "
    '"bensin 50k kemarin". Extract every purchased line item.\n'
    '- "summary_query": the user asks how much they spent (today, this week, '
    'this month). Set "summary_period"; do not compute anything.\n'
    '- "chat": anything else — greetings, questions, or a photo that is not '
    'a receipt. Put a short, friendly answer in Indonesian in "reply".\n\n'
    "Rules for expense_log:\n"
    "- All amounts are whole Indonesian rupiah integers: \"17.500\" -> 17500, "
    '"25rb"/"25k" -> 25000, "1,2jt" -> 1200000.\n'
    "- items[].total is the line total as printed (qty x unit price, after any "
    "per-line discount). Receipt-level discounts go in \"discount\" as a "
    "positive number; tax/service charge in \"tax\".\n"
    "- Skip non-purchase lines: change (kembalian), cash tendered, points, "
    "membership info.\n"
    "- \"total\" is the amount actually paid (often labelled Netto, Grand "
    "Total, or Ttl). On some receipts a line labelled \"Total\" is printed "
    "BEFORE tax/service — that one is the subtotal. Put the sum of all "
    "tax and service charges in \"tax\".\n"
    "- transaction_date is YYYY-MM-DD. Copy the day, month and year exactly "
    "as printed on the receipt; never adjust them toward today's date. "
    'Receipts often write dates as DD/MM/YYYY or DD-Mon-YY, e.g. "26-Jul-26" '
    "is 2026-07-26. Month abbreviations may be English or Indonesian: "
    "Jan, Feb/Peb, Mar, Apr, May/Mei, Jun, Jul, Aug/Agu/Agt, Sep, Oct/Okt, "
    "Nov/Nop, Dec/Des. For a typed message, resolve relative words like "
    '"kemarin" against today\'s date. Use null if no date is known.\n'
    "- payment_method: the method actually used — when a receipt lists both "
    '"Tunai" and "Card", pick the one with an amount next to it.\n'
    "- Pick the single best category for EACH item from this list. Use "
    '"other" only when nothing else fits:\n'
    f"{_CATEGORY_GUIDE}\n"
    "- Never extract card numbers, phone numbers, or people's names.\n"
    "- is_receipt is true only for a photo of a receipt/invoice.\n"
    "- confidence (0..1) is how sure you are that the amounts were read "
    "correctly; use a low value for a blurry, cut-off, or crumpled receipt.\n"
    "For intents other than expense_log, items is [] and the money fields "
    "are null/0."
)

DEFAULT_IMAGE_PROMPT = "Baca struk belanja ini."


def _nullable(schema_type: str) -> dict:
    return {"type": [schema_type, "null"]}


# Strict structured output: the model cannot return a category outside the
# list or skip a field, so parsing failures are rare and always real errors.
EXPENSE_RESPONSE_SCHEMA: dict = {
    "name": "expense_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent", "reply", "is_receipt", "merchant", "transaction_date", "items",
            "subtotal", "discount", "tax", "total", "payment_method", "confidence",
            "summary_period",
        ],
        "properties": {
            "intent": {"type": "string", "enum": ["expense_log", "summary_query", "chat"]},
            "reply": {"type": "string"},
            "is_receipt": {"type": "boolean"},
            "merchant": _nullable("string"),
            "transaction_date": _nullable("string"),
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "qty", "unit_price", "total", "category"],
                    "properties": {
                        "name": {"type": "string"},
                        "qty": {"type": "number"},
                        "unit_price": _nullable("integer"),
                        "total": {"type": "integer"},
                        "category": {"type": "string", "enum": [c.value for c in Category]},
                    },
                },
            },
            "subtotal": _nullable("integer"),
            "discount": {"type": "integer"},
            "tax": {"type": "integer"},
            "total": _nullable("integer"),
            "payment_method": _nullable("string"),
            "confidence": {"type": "number"},
            "summary_period": {"type": ["string", "null"], "enum": ["today", "week", "month", None]},
        },
    },
}


class LLMClientError(RuntimeError):
    """Raised when the upstream LLM call fails."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMClient:
    """Thin client for an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # Tests inject an httpx.MockTransport here to avoid real network calls.
        self._transport = transport

    def _completions_url(self) -> str:
        """Build the chat-completions URL, tolerating a base URL with or without /v1."""
        base = self._settings.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    async def analyze_expense_message(
        self, message: str, today: date, image_url: str | None = None
    ) -> tuple[ExpenseAnalysis, dict]:
        """Classify a Telegram message and, for an expense, extract its items.

        Returns the validated analysis and the raw parsed JSON (stored with
        the receipt for debugging).
        """
        if image_url:
            user_content: str | list[dict] = [
                {"type": "text", "text": message or DEFAULT_IMAGE_PROMPT},
                # "high" detail: small receipt print is unreadable at low detail.
                {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
            ]
        else:
            user_content = message

        system_prompt = EXPENSE_SYSTEM_PROMPT.format(
            today=today.isoformat(), timezone=self._settings.timezone
        )
        body = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": EXPENSE_RESPONSE_SCHEMA},
        }
        response = await self._post_completion(body)
        return self._parse_expense_analysis(response)

    async def _post_completion(self, body: dict) -> httpx.Response:
        """POST a chat-completions request, mapping transport failures to LLMClientError."""
        if not self._settings.api_key:
            raise LLMClientError("API_KEY is not configured on the server.", status_code=503)

        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(self._completions_url(), json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMClientError("The upstream request timed out.", status_code=504) from exc
        except httpx.RequestError as exc:
            raise LLMClientError(f"Could not reach the upstream LLM API: {exc}", status_code=502) from exc

        if response.status_code >= 400:
            # Log the full upstream body for debugging, but do not leak it to users.
            logger.warning("Upstream returned %s: %s", response.status_code, response.text)
            raise LLMClientError(
                self._extract_error(response),
                status_code=502 if response.status_code >= 500 else response.status_code,
            )

        return response

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        """Pull a human-readable message out of an upstream error body."""
        try:
            data = response.json()
        except ValueError:
            return f"Upstream error (HTTP {response.status_code})."

        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(data.get("message"), str):
            return data["message"]
        return f"Upstream error (HTTP {response.status_code})."

    @staticmethod
    def _parse_expense_analysis(response: httpx.Response) -> tuple[ExpenseAnalysis, dict]:
        """Map the structured-output completion onto an ExpenseAnalysis.

        Failure messages here go to error_logs (visible via /logs), never to
        the Telegram user, so they include real diagnostic detail.
        """
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMClientError(
                f"Upstream returned a non-JSON response: {response.text[:300]!r}"
            ) from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMClientError(f"Upstream returned no choices. Body: {data!r}"[:500])

        message = choices[0].get("message") or {}
        if message.get("refusal"):
            raise LLMClientError(f"Model refused: {message['refusal']!r}"[:500])

        content = message.get("content")
        if not content:
            raise LLMClientError(f"Upstream returned an empty message. choices[0]={choices[0]!r}"[:500])

        try:
            parsed = json.loads(content)
            return ExpenseAnalysis.model_validate(parsed), parsed
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Could not parse expense analysis JSON: %s", content)
            raise LLMClientError(
                f"Could not parse LLM JSON ({exc}). Raw content: {content[:300]!r}"
            ) from exc
