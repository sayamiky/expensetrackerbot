"""End-to-end tests for the Telegram webhook with stubbed Telegram + OpenAI."""

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from app.api.routes.telegram import TELEGRAM_SECRET_HEADER, get_llm_client, get_telegram_client
from app.core.config import Settings, get_settings
from app.core.security import reset_rate_limiter
from app.db.session import get_db_sessionmaker
from app.main import app
from app.services import error_log
from app.services.llm_client import LLMClient
from app.services.periods import today_local
from app.services.telegram import TelegramClient

WEBHOOK_URL = f"{get_settings().api_v1_prefix}/telegram/webhook"
SECRET = "webhook-secret"
CHAT = 42

RECEIPT_JSON = {
    "intent": "expense_log",
    "reply": "",
    "is_receipt": True,
    "merchant": "Indomaret Sudirman",
    "transaction_date": None,
    "items": [
        {"name": "Bayam", "qty": 1, "unit_price": 5000, "total": 5000, "category": "vegetable_fruit"},
        {"name": "Keripik", "qty": 2, "unit_price": 7500, "total": 15000, "category": "snack"},
    ],
    "subtotal": 20000,
    "discount": 0,
    "tax": 0,
    "total": 20000,
    "payment_method": "QRIS",
    "confidence": 0.95,
    "summary_period": None,
}


def llm_body(content: dict) -> dict:
    return {"model": "test-model", "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}]}


def text_expense(name: str, total: int, category: str) -> dict:
    return {
        **RECEIPT_JSON,
        "is_receipt": False,
        "merchant": None,
        "payment_method": None,
        "items": [{"name": name, "qty": 1, "unit_price": total, "total": total, "category": category}],
        "subtotal": None,
        "total": None,
    }


def make_settings(**overrides) -> Settings:
    defaults = {
        "api_key": "test-key",
        "model": "test-model",
        "telegram_bot_token": "test-bot-token",
        "telegram_webhook_secret": SECRET,
        # Explicit, so real values in a developer's .env never leak in (a real
        # DATABASE_URL would hit a real database; a real allowlist would
        # silently ignore the test chat).
        "database_url": "",
        "telegram_allowed_chat_ids": "",
    }
    return Settings(**{**defaults, **overrides})


class FakeTelegram:
    """Records every Bot API call and answers like Telegram would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/file/bot" in url:
            return httpx.Response(200, content=b"\xff\xd8\xfffake-jpeg")
        method = url.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else {}
        self.calls.append((method, body))
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/x.jpg"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 500}})

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def last(self, method: str) -> dict:
        return [body for name, body in self.calls if name == method][-1]


def use_webhook(settings: Settings, llm_handler, tg: FakeTelegram, sessionmaker=None) -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm_client] = lambda: LLMClient(settings, transport=httpx.MockTransport(llm_handler))
    app.dependency_overrides[get_telegram_client] = lambda: TelegramClient(
        settings, transport=httpx.MockTransport(tg.handler)
    )
    if sessionmaker is not None:
        app.dependency_overrides[get_db_sessionmaker] = lambda: sessionmaker


def post(client: TestClient, update: dict):
    return client.post(WEBHOOK_URL, json=update, headers={TELEGRAM_SECRET_HEADER: SECRET})


def photo_update(update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "chat": {"id": CHAT},
            "photo": [{"file_id": "small", "width": 90}, {"file_id": "large", "width": 1280}],
        },
    }


def callback_update(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {"id": "cb1", "data": data, "message": {"message_id": 500, "chat": {"id": CHAT}}},
    }


def receipts(db_sessionmaker) -> list:
    async def run():
        async with db_sessionmaker() as session:
            from sqlalchemy import select

            from app.db.models import Receipt

            return list((await session.execute(select(Receipt).order_by(Receipt.id))).scalars())

    return asyncio.run(run())


def setup_function() -> None:
    reset_rate_limiter()


def teardown_function() -> None:
    app.dependency_overrides.clear()
    reset_rate_limiter()


def no_llm(request: httpx.Request) -> httpx.Response:
    raise AssertionError("LLM must not be called")


def test_webhook_requires_secret():
    use_webhook(make_settings(), no_llm, FakeTelegram())
    with TestClient(app) as client:
        response = client.post(WEBHOOK_URL, json={"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "hi"}})
    assert response.status_code == 401


def test_webhook_honours_allowlist(db_sessionmaker):
    tg = FakeTelegram()
    use_webhook(make_settings(telegram_allowed_chat_ids="1"), no_llm, tg, db_sessionmaker)
    with TestClient(app) as client:
        post(client, {"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "hi"}})
    assert tg.calls == []


def test_photo_receipt_becomes_draft_then_confirmed(db_sessionmaker):
    captured: dict = {}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        captured["llm"] = json.loads(request.content)
        return httpx.Response(200, json=llm_body(RECEIPT_JSON))

    tg = FakeTelegram()
    use_webhook(make_settings(), llm_handler, tg, db_sessionmaker)

    with TestClient(app) as client:
        assert post(client, photo_update()).status_code == 200

        # The largest photo went to the model as a data URL, with structured output.
        user_content = captured["llm"]["messages"][1]["content"]
        assert user_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert captured["llm"]["response_format"]["type"] == "json_schema"
        assert tg.last("getFile") == {"file_id": "large"}

        # "⏳" first, then that same message is overwritten with the draft + buttons.
        assert tg.methods() == ["sendMessage", "getFile", "editMessageText"]
        draft = tg.last("editMessageText")
        assert draft["message_id"] == 500
        assert "Indomaret Sudirman" in draft["text"] and "Total: Rp 20.000" in draft["text"]
        buttons = [b["callback_data"] for row in draft["reply_markup"]["inline_keyboard"] for b in row]
        [saved] = receipts(db_sessionmaker)
        assert saved.status == "pending"
        assert buttons == [f"confirm:{saved.id}", f"cancel:{saved.id}", f"editcat:{saved.id}"]

        # A pending draft does not show up in /history yet.
        post(client, {"update_id": 2, "message": {"chat": {"id": CHAT}, "text": "/history"}})
        assert "Belum ada transaksi" in tg.last("sendMessage")["text"]

        post(client, callback_update(3, f"confirm:{saved.id}"))
        assert tg.last("answerCallbackQuery")["text"] == "Tersimpan ✅"
        assert "Struk tersimpan" in tg.last("editMessageText")["text"]

        post(client, {"update_id": 4, "message": {"chat": {"id": CHAT}, "text": "/history"}})
        history = tg.last("sendMessage")["text"]
        assert f"#{saved.id}" in history and "Total: Rp 20.000 (1 transaksi)" in history

    assert receipts(db_sessionmaker)[0].transaction_date == today_local(make_settings().tz)


def test_change_category_from_draft_buttons(db_sessionmaker):
    tg = FakeTelegram()
    use_webhook(make_settings(), lambda r: httpx.Response(200, json=llm_body(RECEIPT_JSON)), tg, db_sessionmaker)

    with TestClient(app) as client:
        post(client, photo_update())
        [saved] = receipts(db_sessionmaker)

        post(client, callback_update(2, f"editcat:{saved.id}"))
        picker = tg.last("editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in picker for b in row]
        assert "Vegetable and Fruit" in labels and "Other" in labels and len(labels) == 14  # 13 + Kembali

        post(client, callback_update(3, f"setcat:{saved.id}:shopping"))
        assert "[Shopping]" in tg.last("editMessageText")["text"]

    [updated] = receipts(db_sessionmaker)
    assert updated.category == "shopping"
    assert {item.category for item in updated.items} == {"shopping"}


def test_cancel_discards_draft(db_sessionmaker):
    tg = FakeTelegram()
    use_webhook(make_settings(), lambda r: httpx.Response(200, json=llm_body(RECEIPT_JSON)), tg, db_sessionmaker)
    with TestClient(app) as client:
        post(client, photo_update())
        [saved] = receipts(db_sessionmaker)
        post(client, callback_update(2, f"cancel:{saved.id}"))
    assert receipts(db_sessionmaker) == []
    assert "dibatalkan" in tg.last("editMessageText")["text"]


def test_low_confidence_receipt_is_rejected(db_sessionmaker):
    tg = FakeTelegram()
    blurry = {**RECEIPT_JSON, "confidence": 0.2}
    use_webhook(make_settings(), lambda r: httpx.Response(200, json=llm_body(blurry)), tg, db_sessionmaker)
    with TestClient(app) as client:
        post(client, photo_update())
    assert receipts(db_sessionmaker) == []
    assert "kurang jelas" in tg.last("editMessageText")["text"]


def test_text_expense_is_saved_immediately_with_undo(db_sessionmaker):
    tg = FakeTelegram()
    use_webhook(
        make_settings(),
        lambda r: httpx.Response(200, json=llm_body(text_expense("Bensin", 50000, "transportation"))),
        tg,
        db_sessionmaker,
    )
    with TestClient(app) as client:
        post(client, {"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "bensin 50rb"}})

    [saved] = receipts(db_sessionmaker)
    assert saved.status == "confirmed" and saved.source == "text"
    sent = tg.last("sendMessage")
    assert sent["text"].startswith(f"✅ Tercatat #{saved.id}: Bensin — Rp 50.000 (Transportation)")
    assert "Total hari ini: Rp 50.000" in sent["text"]
    buttons = [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert f"cancel:{saved.id}" in buttons


def test_duplicate_update_is_processed_once(db_sessionmaker):
    calls = {"llm": 0}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        calls["llm"] += 1
        return httpx.Response(200, json=llm_body(text_expense("Kopi", 18000, "food")))

    use_webhook(make_settings(), llm_handler, FakeTelegram(), db_sessionmaker)
    update = {"update_id": 77, "message": {"chat": {"id": CHAT}, "text": "kopi 18rb"}}
    with TestClient(app) as client:
        post(client, update)
        post(client, update)

    assert calls["llm"] == 1
    assert len(receipts(db_sessionmaker)) == 1


def test_summary_question_uses_database_totals(db_sessionmaker):
    tg = FakeTelegram()
    replies = iter([
        llm_body(text_expense("Makan", 30000, "food")),
        llm_body({**RECEIPT_JSON, "intent": "summary_query", "items": [], "summary_period": "today",
                  "reply": "made up number"}),
    ])
    use_webhook(make_settings(), lambda r: httpx.Response(200, json=next(replies)), tg, db_sessionmaker)
    with TestClient(app) as client:
        post(client, {"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "makan 30rb"}})
        post(client, {"update_id": 2, "message": {"chat": {"id": CHAT}, "text": "hari ini habis berapa?"}})

    text = tg.last("sendMessage")["text"]
    assert "made up number" not in text
    assert "Total: Rp 30.000 (1 transaksi)" in text and "Food: Rp 30.000" in text


def test_llm_failure_overwrites_placeholder_and_is_logged(db_sessionmaker):
    tg = FakeTelegram()
    use_webhook(
        make_settings(), lambda r: httpx.Response(502, json={"error": {"message": "boom"}}), tg, db_sessionmaker
    )
    with TestClient(app) as client:
        assert post(client, photo_update()).status_code == 200

    assert "sibuk" in tg.last("editMessageText")["text"]

    async def read_errors():
        async with db_sessionmaker() as session:
            return await error_log.get_recent_errors(session)

    [entry] = asyncio.run(read_errors())
    assert entry.error_type == "LLMClientError" and entry.chat_id == CHAT


def test_missing_database_sends_friendly_error():
    tg = FakeTelegram()
    use_webhook(make_settings(), no_llm, tg)  # database_url is empty
    with TestClient(app) as client:
        assert post(client, {"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "/history"}}).status_code == 200
    assert "sibuk" in tg.last("sendMessage")["text"]


def test_callback_cannot_touch_another_chats_receipt(db_sessionmaker):
    tg = FakeTelegram()
    use_webhook(make_settings(), lambda r: httpx.Response(200, json=llm_body(RECEIPT_JSON)), tg, db_sessionmaker)
    with TestClient(app) as client:
        post(client, photo_update())
        [saved] = receipts(db_sessionmaker)
        other_chat = callback_update(2, f"cancel:{saved.id}")
        other_chat["callback_query"]["message"]["chat"]["id"] = 999
        post(client, other_chat)

    assert len(receipts(db_sessionmaker)) == 1
    assert "tidak ditemukan" in tg.last("answerCallbackQuery")["text"]
