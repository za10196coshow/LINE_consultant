import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openai import APIError, APITimeoutError, AuthenticationError, OpenAI, RateLimitError
from pydantic import ValidationError

from app.models import Decision, VenueCandidate, VenueCandidatePayload, VenueSearchCriteria, VenueSearchResult
from app.prompts.system import KANJI_PROMPT, SEARCH_PROMPT, VENUE_REPLY_PROMPT

logger = logging.getLogger(__name__)


class OpenAITimeoutExhausted(Exception):
    """Raised after exactly two timed-out decision attempts."""


class AIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        name: str,
        timezone: str,
        timeout_seconds: float = 45.0,
        search_timeout_seconds: float = 75.0,
    ):
        # Disable SDK retries so timeout retry count is explicit and observable.
        self.client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self.search_client = OpenAI(api_key=api_key, timeout=search_timeout_seconds, max_retries=0)
        self.model, self.name, self.timezone = model, name, timezone

    def decide(self, message: str, display_name: str, context: dict) -> Decision:
        now = datetime.now(ZoneInfo(self.timezone)).isoformat()
        instructions = KANJI_PROMPT.format(name=self.name, now=now, timezone=self.timezone)
        payload = {"speaker": display_name, "message": message, "saved_state": context}
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        started = time.monotonic()
        logger.info("OpenAI decision start model=%s", self.model)
        try:
            decision = self._parse_decision(instructions, serialized, max_output_tokens=1600)
            logger.info("OpenAI decision success action=%s elapsed_ms=%d", decision.action.value, _elapsed_ms(started))
            return decision
        except APITimeoutError:
            logger.warning("OpenAI decision timeout elapsed_ms=%d; retrying once", _elapsed_ms(started))
            try:
                decision = self._parse_decision(instructions, serialized, max_output_tokens=1600)
                logger.info("OpenAI decision retry success action=%s elapsed_ms=%d", decision.action.value, _elapsed_ms(started))
                return decision
            except APITimeoutError as exc:
                logger.error("OpenAI decision retry timeout elapsed_ms=%d", _elapsed_ms(started))
                raise OpenAITimeoutExhausted from exc
            except Exception as exc:
                self._log_non_timeout_error("decision retry", exc, started)
                return Decision(action="IGNORE")
        except ValidationError as exc:
            # Structured Output can occasionally be cut off after the model has
            # spent part of the output budget on reasoning. Retry once with a
            # larger budget instead of silently losing the user's facts.
            logger.warning("OpenAI returned incomplete structured output; retrying (%s)", type(exc).__name__)
            try:
                retry_instructions = instructions + "\nJSONを必ず最後まで完成させ、説明を増やさず簡潔に出力すること。"
                decision = self._parse_decision(retry_instructions, serialized, max_output_tokens=2600)
                logger.info("OpenAI structured-output retry success action=%s elapsed_ms=%d", decision.action.value, _elapsed_ms(started))
                return decision
            except Exception as retry_exc:
                logger.error("OpenAI structured-output retry failed (%s)", type(retry_exc).__name__)
                return Decision(action="IGNORE")
        except Exception as exc:
            self._log_non_timeout_error("decision", exc, started)
            return Decision(action="IGNORE")

    @staticmethod
    def _log_non_timeout_error(operation: str, exc: Exception, started: float) -> None:
        if isinstance(exc, AuthenticationError):
            category = "authentication_error"
        elif isinstance(exc, RateLimitError):
            category = "rate_limit"
        elif isinstance(exc, APIError):
            category = "api_error"
        elif isinstance(exc, ValidationError):
            category = "structured_output_error"
        else:
            category = type(exc).__name__
        logger.error("OpenAI %s failed category=%s elapsed_ms=%d", operation, category, _elapsed_ms(started))

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

    def search(self, criteria: VenueSearchCriteria, request: str) -> VenueSearchResult | None:
        started = time.monotonic()
        logger.info("OpenAI web search start model=%s", self.model)
        try:
            result = self._search_once(criteria, request)
            logger.info("OpenAI web search success elapsed_ms=%d", _elapsed_ms(started))
            return result
        except APITimeoutError:
            logger.warning("OpenAI web search timeout elapsed_ms=%d; retrying once", _elapsed_ms(started))
            try:
                result = self._search_once(criteria, request)
                logger.info("OpenAI web search retry success elapsed_ms=%d", _elapsed_ms(started))
                return result
            except APITimeoutError:
                logger.error("OpenAI web search retry timeout elapsed_ms=%d", _elapsed_ms(started))
                return None
            except Exception as exc:
                self._log_non_timeout_error("web search retry", exc, started)
                return None
        except Exception as exc:
            self._log_non_timeout_error("web search", exc, started)
            return None

    def _search_once(self, criteria: VenueSearchCriteria, request: str) -> VenueSearchResult | None:
        location = {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}
        if criteria.location:
            location["city"] = criteria.location
        response = self.search_client.responses.parse(
            model=self.model,
            instructions=SEARCH_PROMPT,
            input=json.dumps(
                {"request": request, "saved_event_conditions": criteria.model_dump(exclude_none=True)},
                ensure_ascii=False,
            ),
            tools=[{"type": "web_search", "user_location": location}],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            max_tool_calls=2,
            max_output_tokens=2200,
            text_format=VenueCandidatePayload,
            store=False,
        )
        source_urls = _extract_source_urls(response)
        parsed = response.output_parsed or VenueCandidatePayload()
        verified: list[VenueCandidate] = []
        for candidate in parsed.candidates:
            if candidate.url in source_urls and candidate.url not in {item.url for item in verified}:
                verified.append(candidate)
            if len(verified) == 3:
                break
        if parsed.candidates and not verified:
            logger.warning("OpenAI web search candidates had no matching source URLs")
        return VenueSearchResult(candidates=verified, source_urls=source_urls)

    def render_venue_reply(self, criteria: VenueSearchCriteria, candidates: list[VenueCandidate], request: str) -> str:
        started = time.monotonic()
        try:
            text = self._render_venue_reply_once(criteria, candidates, request)
        except APITimeoutError:
            logger.warning("OpenAI venue reply timeout; retrying once")
            try:
                text = self._render_venue_reply_once(criteria, candidates, request)
            except Exception as exc:
                self._log_non_timeout_error("venue reply retry", exc, started)
                return _venue_reply_fallback(criteria, candidates)
        except Exception as exc:
            self._log_non_timeout_error("venue reply", exc, started)
            return _venue_reply_fallback(criteria, candidates)
        if not _valid_venue_reply(text, candidates):
            logger.warning("OpenAI venue reply failed URL/content validation; using fallback")
            return _venue_reply_fallback(criteria, candidates)
        return text.strip()

    def _render_venue_reply_once(self, criteria: VenueSearchCriteria, candidates: list[VenueCandidate], request: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            instructions=VENUE_REPLY_PROMPT.format(name=self.name),
            input=json.dumps(
                {
                    "request": request,
                    "event_conditions": criteria.model_dump(exclude_none=True),
                    "verified_candidates": [candidate.model_dump(exclude_none=True) for candidate in candidates],
                },
                ensure_ascii=False,
            ),
            max_output_tokens=1200,
            store=False,
        )
        return response.output_text


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _extract_source_urls(response) -> list[str]:
    if hasattr(response, "model_dump"):
        payload = response.model_dump(mode="json")
    elif isinstance(response, dict):
        payload = response
    else:
        payload = {}
    found: list[str] = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "url" and isinstance(child, str) and _safe_source_url(child) and child not in found:
                    found.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload.get("output", []))
    return found


def _safe_source_url(url: str) -> bool:
    if len(url) > 500:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    blocked_hosts = {"google.com", "www.google.com", "bing.com", "www.bing.com"}
    return parsed.hostname.lower() not in blocked_hosts


def _valid_venue_reply(text: str, candidates: list[VenueCandidate]) -> bool:
    if not text or text.lstrip().startswith(("{", "[")):
        return False
    urls = re.findall(r"https?://[^\s)\]。、,;]+", text)
    expected = [candidate.url for candidate in candidates]
    if sorted(urls) != sorted(expected):
        return False
    prose = text
    for url in urls:
        prose = prose.replace(url, "")
    if len(re.sub(r"[\s①②③④⑤\W]+", "", prose)) < 12:
        return False
    return all(candidate.name in text for candidate in candidates)


def _venue_reply_fallback(criteria: VenueSearchCriteria, candidates: list[VenueCandidate]) -> str:
    if not candidates:
        return "今の条件だと、これって店がうまく拾えなかった🙏\n駅を少し広げるか、予算をちょい上げて探してみる？"
    if len(candidates) == 1:
        item = candidates[0]
        detail = item.reason or item.area or item.budget or "今の条件にはわりと合いそう"
        return f"条件に合いそうなの、今のところここが一番よさそう。\n\n{item.name}\n{detail}\n{item.url}\n\nもう少し広げて探してみる？"
    intro_area = f"{criteria.location}で" if criteria.location else "条件に合わせて"
    lines = [f"{intro_area}見てみたけど、このへん良さそう！"]
    for index, item in enumerate(candidates, start=1):
        detail = item.reason or item.budget or item.area or "みんなで行く候補によさそう"
        lines.extend(["", f"{'①②③'[index - 1]} {item.name}", detail, item.url])
    lines.extend(["", "俺なら①かな。この候補でみんなに聞いてみる？"])
    return "\n".join(lines)
