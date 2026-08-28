import httpx
import pytest
from openai import APITimeoutError

from app.ai.client import AIClient, OpenAITimeoutExhausted
from app.models import Action, Decision, VenueSearchCriteria


class BrokenResponses:
    def parse(self, **kwargs):
        raise TimeoutError("timeout")


class BrokenOpenAI:
    responses = BrokenResponses()


def test_openai_exception_returns_ignore():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = BrokenOpenAI()
    assert ai.decide("雑談", "A", {}).action == Action.IGNORE


class ParsedResponse:
    def __init__(self, decision):
        self.output_parsed = decision


class ValidResponses:
    def __init__(self):
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        return ParsedResponse(Decision(action=Action.REPLY, reply_text="了解"))


class ValidOpenAI:
    def __init__(self):
        self.responses = ValidResponses()


def timeout_error():
    return APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


def test_openai_normal_response():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = ValidOpenAI()
    assert ai.decide("今どんな感じ？", "A", {}).action == Action.REPLY
    assert ai.client.responses.calls == 1


def test_client_uses_explicit_timeout_without_hidden_sdk_retries():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo", timeout_seconds=45, search_timeout_seconds=75)
    assert ai.client.timeout == 45
    assert ai.client.max_retries == 0
    assert ai.search_client.timeout == 75
    assert ai.search_client.max_retries == 0


class TimeoutThenSuccessResponses:
    def __init__(self):
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise timeout_error()
        return ParsedResponse(Decision(action=Action.REMEMBER_ONLY))


class TimeoutThenSuccessOpenAI:
    def __init__(self):
        self.responses = TimeoutThenSuccessResponses()


def test_timeout_then_retry_success():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = TimeoutThenSuccessOpenAI()
    assert ai.decide("19日行ける", "A", {}).action == Action.REMEMBER_ONLY
    assert ai.client.responses.calls == 2


class AlwaysTimeoutResponses:
    def __init__(self):
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        raise timeout_error()


class AlwaysTimeoutOpenAI:
    def __init__(self):
        self.responses = AlwaysTimeoutResponses()


def test_timeout_retry_also_fails():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = AlwaysTimeoutOpenAI()
    with pytest.raises(OpenAITimeoutExhausted):
        ai.decide("今どんな感じ？", "A", {})
    assert ai.client.responses.calls == 2


class TruncatedThenValidResponses:
    def __init__(self):
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            Decision.model_validate_json('{"action":"REMEMBER_ONLY","facts":[{"candidate_date":"9/19","availability":"yes","note":')
        return ParsedResponse(Decision(action=Action.REMEMBER_ONLY))


class RecoveringOpenAI:
    def __init__(self):
        self.responses = TruncatedThenValidResponses()


def test_truncated_structured_output_retries_with_larger_budget():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RecoveringOpenAI()

    decision = ai.decide("19日なら行ける", "田中", {})

    assert decision.action == Action.REMEMBER_ONLY
    assert len(ai.client.responses.calls) == 2
    assert ai.client.responses.calls[0]["max_output_tokens"] == 1600
    assert ai.client.responses.calls[1]["max_output_tokens"] == 2600


class WebSearchResponse:
    output_text = (
        "探してみた！\n① 店A\nhttps://venue.example/a\n② 店B\nhttps://venue.example/b\n"
        "③ 店C\nhttps://venue.example/c\n④ 架空店\nhttps://fake.example/invented"
    )

    def model_dump(self, mode="json"):
        return {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"url": "https://venue.example/a"},
                            {"url": "https://venue.example/b"},
                            {"url": "https://venue.example/c"},
                        ]
                    },
                }
            ]
        }


class WebResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class WebOpenAI:
    def __init__(self, outcomes):
        self.responses = WebResponses(outcomes)


def test_web_search_uses_builtin_tool_sources_and_removes_invented_url():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.search_client = WebOpenAI([WebSearchResponse()])
    criteria = VenueSearchCriteria(location="横浜", party_size=5, budget_max=5000, genre="焼肉")

    result = ai.search(criteria, "店探して")

    assert result is not None
    assert "https://venue.example/a" in result.text
    assert "https://fake.example/invented" not in result.text
    assert "架空店" not in result.text
    assert result.venues_found == 3
    call = ai.search_client.responses.calls[0]
    assert call["tools"][0]["type"] == "web_search"
    assert call["tools"][0]["user_location"]["country"] == "JP"
    assert call["tools"][0]["user_location"]["city"] == "横浜"
    assert call["tool_choice"] == "required"
    assert call["include"] == ["web_search_call.action.sources"]


def test_web_search_timeout_retries_once_then_succeeds():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.search_client = WebOpenAI([timeout_error(), WebSearchResponse()])

    assert ai.search(VenueSearchCriteria(location="横浜"), "店探して") is not None
    assert len(ai.search_client.responses.calls) == 2


def test_web_search_timeout_retry_failure_returns_none():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.search_client = WebOpenAI([timeout_error(), timeout_error()])

    assert ai.search(VenueSearchCriteria(location="横浜"), "店探して") is None
    assert len(ai.search_client.responses.calls) == 2
