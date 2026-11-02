"""Minimal Pydantic models for Telegram webhook updates."""

from pydantic import BaseModel, Field


class TelegramChat(BaseModel):
    """The chat the message came from. Group chats use negative ids."""

    id: int


class TelegramPhotoSize(BaseModel):
    """One size of a photo in a Telegram message."""

    file_id: str
    file_unique_id: str | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None


class TelegramMessage(BaseModel):
    """The parts of a Telegram message we care about."""

    message_id: int | None = None
    text: str | None = Field(
        default=None,
        description="Message text; None for non-text messages (photos, stickers...).",
    )
    caption: str | None = Field(
        default=None,
        description="Caption attached to a photo/document message.",
    )
    chat: TelegramChat | None = None
    photo: list[TelegramPhotoSize] | None = Field(
        default=None,
        description="Present for photo messages; several sizes, largest last.",
    )


class TelegramCallbackQuery(BaseModel):
    """A tap on an inline keyboard button."""

    id: str
    data: str | None = None
    # The bot message the button was attached to.
    message: TelegramMessage | None = None


class TelegramUpdate(BaseModel):
    """A single update pushed by Telegram to the webhook."""

    update_id: int
    message: TelegramMessage | None = Field(
        default=None,
        description="Absent for edits, channel posts, callback queries, etc.",
    )
    callback_query: TelegramCallbackQuery | None = None
