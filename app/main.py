import base64
import hashlib
import hmac
import json
import logging
import sys

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app.ai.budget import ApiBudget
from app.ai.client import AIClient
from app.config import get_settings
from app.line.client import LineClient
from app.repositories.database import Database
from app.services.kanji import KanjiService

settings = get_settings()
if sys.version_info[:2] != (3, 12):
    raise RuntimeError(f"Python 3.12 is required; running {sys.version.split()[0]}")
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
settings.ensure_database_directory()
db = Database(settings.database_path)
api_budget = ApiBudget(
    db,
    settings.daily_api_budget_jpy,
    settings.daily_api_stop_threshold_jpy,
    settings.usd_jpy_rate,
    settings.timezone,
)
service = KanjiService(
    db,
    AIClient(
        settings.openai_api_key,
        settings.openai_model,
        settings.ai_kanji_name,
        settings.timezone,
        settings.openai_timeout_seconds,
        settings.openai_search_timeout_seconds,
        api_budget,
    ),
    LineClient(settings.line_channel_access_token),
)
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
async def webhook(request: Request, background_tasks: BackgroundTasks, x_line_signature: str | None = Header(default=None)):
    body = await request.body()
    if not valid_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    for event in payload.get("events", []):
        background_tasks.add_task(service.handle_safely, event)
    return {"ok": True}
