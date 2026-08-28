import base64
import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Header, HTTPException, Request

from app.ai.client import AIClient
from app.config import get_settings
from app.line.client import LineClient
from app.repositories.database import Database
from app.services.kanji import KanjiService

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings.ensure_database_directory()
db = Database(settings.database_path)
service = KanjiService(db, AIClient(settings.openai_api_key, settings.openai_model, settings.ai_kanji_name, settings.timezone), LineClient(settings.line_channel_access_token))
app = FastAPI(title="LINE AI Kanji")


def valid_signature(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    digest = hmac.new(settings.line_channel_secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


@app.get("/")
def root():
    return {"service": "LINE AI Kanji", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str | None = Header(default=None)):
    body = await request.body()
    if not valid_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    for event in payload.get("events", []):
        try:
            service.handle(event)
        except Exception:
            logging.getLogger(__name__).exception("Webhook event processing failed")
    return {"ok": True}

