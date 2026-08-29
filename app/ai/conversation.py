import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import APITimeoutError, OpenAI

from app.ai.budget import ApiBudget, ApiBudgetExceeded
from app.ai.client import _extract_source_urls
from app.models import ConversationDecision, ConversationResearch
from app.prompts.system import (
    CONVERSATION_ASSISTANT_PROMPT,
    CONVERSATION_RESEARCH_PROMPT,
    CONVERSATION_RESEARCH_REPLY_PROMPT,
)

logger = logging.getLogger(__name__)


class ConversationAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        name: str,
        timezone: str,
        budget: ApiBudget,
        timeout_seconds: float = 45.0,
        search_timeout_seconds: float = 75.0,
    ):
        self.client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self.search_client = OpenAI(api_key=api_key, timeout=search_timeout_seconds, max_retries=0)
        self.model, self.name, self.timezone, self.budget = model, name, timezone, budget

    def analyze(self, message: str, display_name: str, context: dict) -> ConversationDecision:
        payload = {
            "now": datetime.now(ZoneInfo(self.timezone)).isoformat(),
            "speaker": display_name,
            "message": message,
            "conversation": context,
        }
        try:
            return self._analyze_once(payload)
        except APITimeoutError:
            logger.warning("Conversation assistant analysis timeout; retrying once")
            try:
                return self._analyze_once(payload)
            except ApiBudgetExceeded:
                raise
            except Exception as exc:
                logger.error("Conversation assistant analysis retry failed category=%s", type(exc).__name__)
                return ConversationDecision()
        except ApiBudgetExceeded:
            raise
        except Exception as exc:
            logger.error("Conversation assistant analysis failed category=%s", type(exc).__name__)
            return ConversationDecision()

    def _analyze_once(self, payload: dict) -> ConversationDecision:
        response = self.budget.execute(
            operation="conversation_analysis",
            model=self.model,
            request=lambda: self.client.responses.parse(
                model=self.model,
                instructions=CONVERSATION_ASSISTANT_PROMPT.format(name=self.name),
                input=json.dumps(payload, ensure_ascii=False, default=str),
                text_format=ConversationDecision,
                max_output_tokens=1800,
                store=False,
            ),
        )
        return response.output_parsed or ConversationDecision()

    def research(self, message: str, context: dict) -> ConversationResearch | None:
        try:
            return self._research_once(message, context)
        except APITimeoutError:
            logger.warning("Conversation assistant web search timeout; retrying once")
            try:
                return self._research_once(message, context)
            except ApiBudgetExceeded:
                raise
            except Exception as exc:
                logger.error("Conversation assistant web search retry failed category=%s", type(exc).__name__)
                return None
        except ApiBudgetExceeded:
            raise
        except Exception as exc:
            logger.error("Conversation assistant web search failed category=%s", type(exc).__name__)
            return None

    def _research_once(self, message: str, context: dict) -> ConversationResearch:
        response = self.budget.execute(
            operation="conversation_web_search",
            model=self.model,
            request=lambda: self.search_client.responses.parse(
                model=self.model,
                instructions=CONVERSATION_RESEARCH_PROMPT,
                input=json.dumps({"request": message, "conversation": context}, ensure_ascii=False, default=str),
                tools=[{"type": "web_search", "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}}],
                tool_choice="required",
                include=["web_search_call.action.sources"],
                max_tool_calls=2,
                max_output_tokens=1800,
                text_format=ConversationResearch,
                store=False,
            ),
        )
        source_urls = set(_extract_source_urls(response))
        parsed = response.output_parsed or ConversationResearch(answer_summary="情報を確認できなかった")
        parsed.sources = [source for source in parsed.sources if source.url in source_urls][:3]
        return parsed

    def render_research_reply(self, message: str, research: ConversationResearch) -> str:
        payload = {"request": message, "research": research.model_dump(exclude_none=True)}
        try:
            text = self._render_research_reply_once(payload)
        except APITimeoutError:
            logger.warning("Conversation research reply timeout; retrying once")
            try:
                text = self._render_research_reply_once(payload)
            except ApiBudgetExceeded:
                raise
            except Exception as exc:
                logger.error("Conversation research reply retry failed category=%s", type(exc).__name__)
                return _research_fallback(research)
        except ApiBudgetExceeded:
            raise
        except Exception as exc:
            logger.error("Conversation research reply failed category=%s", type(exc).__name__)
            return _research_fallback(research)
        return text if _valid_research_reply(text, research) else _research_fallback(research)

    def _render_research_reply_once(self, payload: dict) -> str:
        response = self.budget.execute(
            operation="conversation_research_reply",
            model=self.model,
            request=lambda: self.client.responses.create(
                model=self.model,
                instructions=CONVERSATION_RESEARCH_REPLY_PROMPT.format(name=self.name),
                input=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=900,
                store=False,
            ),
        )
        return response.output_text.strip()


def _valid_research_reply(text: str, research: ConversationResearch) -> bool:
    if not text or text.startswith(("{", "[")):
        return False
    urls = re.findall(r"https?://[^\s)\]。、,;]+", text)
    expected = [source.url for source in research.sources]
    prose = text
    for url in urls:
        prose = prose.replace(url, "")
    return sorted(urls) == sorted(expected) and len(re.sub(r"\W", "", prose)) >= 10


def _research_fallback(research: ConversationResearch) -> str:
    if not research.sources:
        return "今の情報だと、確かなところまで拾えなかった🙏 もう少し条件を足してくれたら探し直せるよ。"
    lines = [_without_urls(research.answer_summary)]
    for source in research.sources:
        lines.extend(["", f"・{source.title}", _without_urls(source.note), source.url])
    return "\n".join(lines)


def _without_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "", text).strip()
