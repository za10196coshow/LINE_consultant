import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI
from pydantic import ValidationError

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
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            return self._parse_decision(instructions, serialized, max_output_tokens=1600)
        except ValidationError as exc:
            # Structured Output can occasionally be cut off after the model has
            # spent part of the output budget on reasoning. Retry once with a
            # larger budget instead of silently losing the user's facts.
            logger.warning("OpenAI returned incomplete structured output; retrying (%s)", type(exc).__name__)
            try:
                retry_instructions = instructions + "\nJSONを必ず最後まで完成させ、説明を増やさず簡潔に出力すること。"
                return self._parse_decision(retry_instructions, serialized, max_output_tokens=2600)
            except Exception as retry_exc:
                logger.error("OpenAI structured-output retry failed (%s)", type(retry_exc).__name__)
                return Decision(action="IGNORE")
        except Exception as exc:
            logger.error("OpenAI decision request failed (%s)", type(exc).__name__)
            return Decision(action="IGNORE")

    def _parse_decision(self, instructions: str, serialized_input: str, max_output_tokens: int) -> Decision:
        response = self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=serialized_input,
            text_format=Decision,
            max_output_tokens=max_output_tokens,
            store=False,
        )
        return response.output_parsed or Decision(action="IGNORE")

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
