from dataclasses import dataclass


@dataclass
class BotReply:
    """What the webhook should send back for one message."""

    text: str
    reply_markup: dict | None = None
    # (filename, bytes) for /export; sent with sendDocument after the text.
    document: tuple[str, bytes] | None = None


@dataclass
class CallbackOutcome:
    """What to do after a button tap.

    ``text`` None means keep the message text and only swap its buttons
    (Telegram rejects an edit that leaves the text unchanged).
    """

    toast: str | None = None
    text: str | None = None
    reply_markup: dict | None = None
