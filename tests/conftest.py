import base64
import hashlib
import hmac
import json
import os

os.environ.setdefault("LINE_CHANNEL_SECRET", "test-secret")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_PATH", ":memory:")

import pytest
from fastapi.testclient import TestClient

from app.ai.budget import ApiBudget
from app.main import app, conversation_assistant, coordinator, service
from app.repositories.database import Database


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    database = Database(":memory:")
    monkeypatch.setattr(service, "db", database)
    monkeypatch.setattr(service.ai, "budget", ApiBudget(database, 100, 90, 150))
    monkeypatch.setattr(coordinator, "db", database)
    monkeypatch.setattr(conversation_assistant, "db", database)
    monkeypatch.setattr(conversation_assistant.ai, "budget", service.ai.budget)
    return database


@pytest.fixture
def client():
    return TestClient(app)


def signed(body: dict):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hmac.new(b"test-secret", raw, hashlib.sha256).digest()
    return raw, base64.b64encode(digest).decode()


@pytest.fixture
def post_webhook(client):
    def post(body):
        raw, signature = signed(body)
        return client.post("/webhook", content=raw, headers={"x-line-signature": signature, "content-type": "application/json"})

    return post


@pytest.fixture
def event_factory():
    def make(message_id="m1", group_id="G1", text="9月飲もうぜ", user_id="U1", event_id=None):
        return {
            "destination": "bot",
            "events": [
                {
                    "type": "message",
                    "webhookEventId": event_id or f"evt-{message_id}",
                    "timestamp": 1788000000000,
                    "replyToken": f"reply-{message_id}",
                    "source": {"type": "group", "groupId": group_id, "userId": user_id},
                    "message": {"id": message_id, "type": "text", "text": text},
                }
            ],
        }

    return make
