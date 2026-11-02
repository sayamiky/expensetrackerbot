"""Categories and the shape of what the LLM extracts from a message or receipt."""

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Category(str, Enum):
    """The fixed category list. The value is what is stored in the database."""

    WORK = "work"
    HOUSE = "house"
    SHOPPING = "shopping"
    TRANSPORTATION = "transportation"
    FOOD = "food"
    SNACK = "snack"
    VEGETABLE_FRUIT = "vegetable_fruit"
    HEALTH = "health"
    ENTERTAINMENT = "entertainment"
    SUBSCRIPTION = "subscription"
    ELECTRONIC = "electronic"
    DONATION = "donation"
    OTHER = "other"


CATEGORY_LABELS: dict[Category, str] = {
    Category.WORK: "Work",
    Category.HOUSE: "House",
    Category.SHOPPING: "Shopping",
    Category.TRANSPORTATION: "Transportation",
    Category.FOOD: "Food",
    Category.SNACK: "Snack",
    Category.VEGETABLE_FRUIT: "Vegetable and Fruit",
    Category.HEALTH: "Health",
    Category.ENTERTAINMENT: "Entertainment",
    Category.SUBSCRIPTION: "Subscription",
    Category.ELECTRONIC: "Electronic",
    Category.DONATION: "Donation",
    Category.OTHER: "Other",
}


def category_label(value: str) -> str:
    """Display label for a stored category slug; unknown slugs pass through."""
    try:
        return CATEGORY_LABELS[Category(value)]
    except ValueError:
        return value


def parse_category(raw: str) -> Category | None:
    """Accept a slug ("vegetable_fruit") or a label ("Vegetable and Fruit"), any case."""
    cleaned = raw.strip().lower()
    for category, label in CATEGORY_LABELS.items():
        if cleaned in {category.value, label.lower(), category.value.replace("_", " ")}:
            return category
    return None


Intent = Literal["expense_log", "summary_query", "chat"]
SummaryPeriod = Literal["today", "week", "month"]


def _round_money(value: object) -> object:
    """The model sometimes returns 17500.0 or -2000 for a discount; money
    columns are whole, non-negative rupiah."""
    if isinstance(value, float):
        value = round(value)
    if isinstance(value, int):
        return abs(value)
    return value


class ExpenseItem(BaseModel):
    """One line extracted from a receipt, or one typed expense."""

    name: str = Field(min_length=1, max_length=200)
    qty: float = Field(gt=0, le=100_000, default=1)
    unit_price: int | None = Field(default=None, ge=0, le=10_000_000_000)
    total: int = Field(ge=0, le=10_000_000_000)
    category: Category = Category.OTHER

    @field_validator("unit_price", "total", mode="before")
    @classmethod
    def _round(cls, value: object) -> object:
        return _round_money(value)

    @field_validator("qty", mode="before")
    @classmethod
    def _default_qty(cls, value: object) -> object:
        return 1 if value in (None, 0) else value


class ExpenseAnalysis(BaseModel):
    """What the LLM decided about one Telegram message.

    Only the "chat" intent uses ``reply``; for the other two the app builds the
    message itself from validated numbers or the real database totals.
    """

    intent: Intent
    reply: str = ""
    is_receipt: bool = False
    merchant: str | None = Field(default=None, max_length=200)
    transaction_date: date | None = None
    items: list[ExpenseItem] = Field(default_factory=list)
    subtotal: int | None = Field(default=None, ge=0)
    discount: int = Field(default=0, ge=0)
    tax: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    payment_method: str | None = Field(default=None, max_length=50)
    confidence: float = Field(default=1.0, ge=0, le=1)
    summary_period: SummaryPeriod | None = None

    @field_validator("subtotal", "total", "discount", "tax", mode="before")
    @classmethod
    def _round(cls, value: object) -> object:
        return _round_money(value)

    @field_validator("discount", "tax", mode="before")
    @classmethod
    def _none_is_zero(cls, value: object) -> object:
        return 0 if value is None else value

    @field_validator("transaction_date", mode="before")
    @classmethod
    def _lenient_date(cls, value: object) -> object:
        """A date the model garbled becomes "unknown" (today) rather than
        failing the whole receipt."""
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError:
                return None
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> object:
        if isinstance(value, (int, float)):
            return min(1.0, max(0.0, float(value)))
        return value
