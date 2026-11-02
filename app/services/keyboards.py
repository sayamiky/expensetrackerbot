"""Inline keyboards and the callback_data they carry.

callback_data formats (Telegram caps them at 64 bytes):
    confirm:<id>          save a draft
    cancel:<id>           discard a draft / undo a saved expense
    editcat:<id>          show the category picker
    setcat:<id>:<slug>    move every item of the receipt to <slug>
    back:<id>             leave the category picker
"""

from app.schemas.expense import CATEGORY_LABELS

CATEGORY_COLUMNS = 3


def _button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def draft_keyboard(receipt_id: int) -> dict:
    return {
        "inline_keyboard": [
            [_button("✅ Simpan", f"confirm:{receipt_id}"), _button("❌ Batal", f"cancel:{receipt_id}")],
            [_button("✏️ Ubah kategori", f"editcat:{receipt_id}")],
        ]
    }


def saved_keyboard(receipt_id: int) -> dict:
    """Buttons under a typed expense that was saved straight away."""
    return {
        "inline_keyboard": [
            [_button("✏️ Ubah kategori", f"editcat:{receipt_id}"), _button("↩️ Batalkan", f"cancel:{receipt_id}")],
        ]
    }


def category_keyboard(receipt_id: int) -> dict:
    buttons = [
        _button(label, f"setcat:{receipt_id}:{category.value}") for category, label in CATEGORY_LABELS.items()
    ]
    rows = [buttons[i : i + CATEGORY_COLUMNS] for i in range(0, len(buttons), CATEGORY_COLUMNS)]
    rows.append([_button("⬅️ Kembali", f"back:{receipt_id}")])
    return {"inline_keyboard": rows}


def parse_callback(data: str) -> tuple[str, int, str | None] | None:
    """"setcat:12:food" -> ("setcat", 12, "food"); None for anything malformed."""
    parts = data.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        receipt_id = int(parts[1])
    except ValueError:
        return None
    return parts[0], receipt_id, parts[2] if len(parts) == 3 else None
