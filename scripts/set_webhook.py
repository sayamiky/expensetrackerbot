"""Register the Telegram webhook and the "/" command menu.

Usage (from the project root, with the venv active):

    python -m scripts.set_webhook https://expense-track-bot.vercel.app

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from the environment or
.env, points Telegram at ``<base_url>/api/v1/telegram/webhook``, and
registers BOT_COMMANDS with setMyCommands.
"""

import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.services.commands import BOT_COMMANDS
from app.services.telegram import TelegramBotError, TelegramClient


async def _register(base_url: str, secret_token: str) -> None:
    settings = get_settings()
    client = TelegramClient(settings)
    webhook_url = f"{base_url.rstrip('/')}{settings.api_v1_prefix}/telegram/webhook"
    print(f"Registering webhook -> {webhook_url}")
    result = await client.set_webhook(webhook_url, secret_token=secret_token)
    print("setWebhook result:", result)
    await client.set_my_commands(BOT_COMMANDS)
    print(f"setMyCommands: registered {len(BOT_COMMANDS)} commands")


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Telegram webhook.")
    parser.add_argument("base_url", help="Public base URL, e.g. https://expense-track-bot.vercel.app")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.telegram_webhook_secret:
        print(
            "Warning: TELEGRAM_WEBHOOK_SECRET is empty; the webhook endpoint will "
            "refuse every request. Set it first.",
            file=sys.stderr,
        )

    try:
        asyncio.run(_register(args.base_url, settings.telegram_webhook_secret))
    except TelegramBotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
