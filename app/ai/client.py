import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.models import Decision
from app.prompts.system import KANJI_PROMPT, SEARCH_PROMPT

logger = logging.getLogger(__name__)


class AIClient:
    def __init__(self, api_key: str, model: str, name: str, timezone: str):
        self.client = OpenAI(api_key=api_key, timeout=20.0, max_retries=1)
        self.model, self.name, self.timezone = model, name, timezone

    def decide(self, message: str, display_name: str, context: dict) -> Decision:
        now = datetime.now(ZoneInfo(self.timezone)).isoformat()
        instructions = KANJI_PROMPT.format(name=self.name, now=now, timezone=self.timezone)
        payload = {"speaker": display_name, "message": message, "saved_state": context}
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False, default=str),
                text_format=Decision,
                max_output_tokens=700,
                store=False,
            )
            return response.output_parsed or Decision(action="IGNORE")
        except Exception:
            logger.exception("OpenAI decision request failed")
            return Decision(action="IGNORE")

    def search(self, context: dict, request: str) -> str | None:
        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=SEARCH_PROMPT,
                input=json.dumps({"request": request, "saved_state": context}, ensure_ascii=False, default=str),
                tools=[{"type": "web_search"}],
                max_output_tokens=1200,
                store=False,
            )
            return response.output_text.strip() or None
        except Exception:
            logger.exception("OpenAI web search failed")
            return None

