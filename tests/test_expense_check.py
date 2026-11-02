"""Tests for receipt validation and amount allocation."""

from datetime import date

import pytest

from app.schemas.expense import Category, ExpenseAnalysis, parse_category
from app.services.expense_check import ExpenseRejected, allocate, check_expense

TODAY = date(2026, 11, 2)


def receipt(**overrides) -> ExpenseAnalysis:
    data = {
        "intent": "expense_log",
        "is_receipt": True,
        "merchant": "Indomaret",
        "transaction_date": "2026-11-01",
        "items": [
            {"name": "Bayam", "qty": 1, "unit_price": 5000, "total": 5000, "category": "vegetable_fruit"},
            {"name": "Keripik", "qty": 2, "unit_price": 7500, "total": 15000, "category": "snack"},
        ],
        "subtotal": 20000,
        "discount": 2000,
        "tax": 0,
        "total": 18000,
        "confidence": 0.9,
    }
    return ExpenseAnalysis.model_validate({**data, **overrides})


def test_allocate_sums_exactly_to_total():
    shares = allocate([5000, 15000], 18000)
    assert shares == [4500, 13500]
    assert sum(allocate([333, 333, 334], 1000)) == 1000
    assert sum(allocate([1, 1, 1], 100)) == 100


def test_check_spreads_discount_over_items_and_picks_dominant_category():
    checked = check_expense(receipt(), TODAY, "photo", 0.5)
    assert checked.total == 18000
    assert checked.net_amounts == [4500, 13500]
    assert checked.category == Category.SNACK
    assert checked.transaction_date == date(2026, 11, 1)
    assert checked.warnings == []


def test_check_warns_when_items_do_not_add_up():
    checked = check_expense(receipt(subtotal=30000, total=28000), TODAY, "photo", 0.5)
    assert any("subtotal" in warning for warning in checked.warnings)


def test_check_warns_when_total_does_not_match():
    checked = check_expense(receipt(total=25000), TODAY, "photo", 0.5)
    assert any("total struk" in warning for warning in checked.warnings)
    # The printed total wins; the user sees the warning on the draft.
    assert checked.total == 25000


def test_check_fills_missing_totals_and_date():
    checked = check_expense(
        receipt(subtotal=None, total=None, discount=0, transaction_date=None), TODAY, "photo", 0.5
    )
    assert checked.total == 20000
    assert checked.transaction_date == TODAY


def test_future_date_falls_back_to_today():
    checked = check_expense(receipt(transaction_date="2027-01-01"), TODAY, "photo", 0.5)
    assert checked.transaction_date == TODAY
    assert checked.warnings


def test_rejects_low_confidence_photo():
    with pytest.raises(ExpenseRejected, match="kurang jelas"):
        check_expense(receipt(confidence=0.2), TODAY, "photo", 0.5)


def test_rejects_photo_that_is_not_a_receipt():
    with pytest.raises(ExpenseRejected, match="bukan foto struk"):
        check_expense(receipt(is_receipt=False), TODAY, "photo", 0.5)


def test_text_expense_does_not_need_is_receipt():
    analysis = receipt(is_receipt=False, items=[{"name": "Bensin", "total": 50000, "category": "transportation"}],
                       subtotal=None, discount=0, total=None)
    checked = check_expense(analysis, TODAY, "text", 0.5)
    assert checked.total == 50000


def test_rejects_expense_without_amount():
    with pytest.raises(ExpenseRejected):
        check_expense(receipt(items=[]), TODAY, "text", 0.5)


def test_money_fields_are_normalised():
    analysis = receipt(discount=-2000.0, items=[{"name": "Teh", "total": 4999.6, "category": "food"}])
    assert analysis.discount == 2000
    assert analysis.items[0].total == 5000


def test_parse_category_accepts_slug_and_label():
    assert parse_category("vegetable_fruit") is Category.VEGETABLE_FRUIT
    assert parse_category("Vegetable and Fruit") is Category.VEGETABLE_FRUIT
    assert parse_category("FOOD") is Category.FOOD
    assert parse_category("groceries") is None
