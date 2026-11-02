from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "expense_track_bot"
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # Interactive docs leak the schema and invite casual probing; keep them off
    # once the API is reachable from the internet.
    docs_enabled: bool = True

    # Per-chat sliding window, applied in-process.
    rate_limit_requests: int = 20
    rate_limit_window_seconds: float = 60.0

    # OpenAI-compatible upstream configuration. Kept below Vercel's 60s
    # function limit so a slow receipt read still leaves time to reply.
    base_url: str = "https://api.openai.com"
    api_key: str = ""
    model: str = "gpt-4o"
    timeout: float = 45.0

    # Every "today", "this week" and "this month" is computed in this zone.
    # Asia/Makassar is WITA (UTC+8).
    timezone: str = "Asia/Makassar"

    # A photo read below this confidence is rejected instead of saved as a
    # draft — a blurry receipt is better re-shot than silently mis-logged.
    min_receipt_confidence: float = 0.5

    # Warn once a category's spending this month reaches this share of its budget.
    budget_warning_ratio: float = 0.8

    # Async SQLAlchemy connection string, e.g. a Neon postgresql://... URL.
    # Empty means every DB-backed part of the webhook replies "busy".
    database_url: str = ""

    # Telegram bot integration (webhook mode).
    # Token from @BotFather; kept secret, never committed.
    telegram_bot_token: str = ""
    # Secret passed back by Telegram in the X-Telegram-Bot-Api-Secret-Token
    # header. Must match the value given to setWebhook, otherwise the webhook
    # endpoint refuses every request.
    telegram_webhook_secret: str = ""
    # Comma-separated chat IDs allowed to talk to the bot. Empty allows any
    # chat. Group chat IDs are negative integers.
    telegram_allowed_chat_ids: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    @property
    def telegram_chat_id_set(self) -> frozenset[int]:
        """Allowed Telegram chat IDs; empty means no restriction."""
        ids: set[int] = set()
        for item in _split_csv(self.telegram_allowed_chat_ids):
            try:
                ids.add(int(item))
            except ValueError:
                continue
        return frozenset(ids)


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated setting, dropping blanks and surrounding space."""
    return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the .env file is read only once."""
    return Settings()
