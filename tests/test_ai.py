import httpx
import pytest
from openai import APITimeoutError

from app.ai.client import AIClient, OpenAITimeoutExhausted
from app.models import Action, Decision, VenueCandidate, VenueCandidatePayload, VenueSearchCriteria
from app.prompts.system import BOT_PERSONA, KANJI_PROMPT, VENUE_REPLY_PROMPT


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
    output_parsed = VenueCandidatePayload(
        candidates=[
            VenueCandidate(name="店A", area="横浜", budget="5千円前後", reason="駅近", url="https://venue.example/a"),
            VenueCandidate(name="店B", area="横浜", budget="4千円台", reason="落ち着く", url="https://venue.example/b"),
            VenueCandidate(name="店C", area="横浜", budget="5千円前後", reason="ワイワイ", url="https://venue.example/c"),
            VenueCandidate(name="架空店", reason="未確認", url="https://fake.example/invented"),
        ]
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

    def parse(self, **kwargs):
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
    assert [candidate.name for candidate in result.candidates] == ["店A", "店B", "店C"]
    assert all(candidate.url in result.source_urls for candidate in result.candidates)
    assert result.venues_found == 3
    call = ai.search_client.responses.calls[0]
    assert call["tools"][0]["type"] == "web_search"
    assert call["tools"][0]["user_location"]["country"] == "JP"
    assert call["tools"][0]["user_location"]["city"] == "横浜"
    assert call["tool_choice"] == "required"
    assert call["include"] == ["web_search_call.action.sources"]
    assert call["text_format"] is VenueCandidatePayload


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


class TextResponse:
    def __init__(self, text):
        self.output_text = text


class RenderResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return TextResponse(outcome)


class RenderOpenAI:
    def __init__(self, outcomes):
        self.responses = RenderResponses(outcomes)


def venue_candidates(count=3):
    all_candidates = [
        VenueCandidate(name="焼肉A", area="横浜駅", budget="5千円前後", reason="駅近で集まりやすい", url="https://venue.example/a"),
        VenueCandidate(name="焼肉B", area="横浜西口", budget="4千円台", reason="少し落ち着いた感じ", url="https://venue.example/b"),
        VenueCandidate(name="焼肉C", area="横浜東口", budget="5千円前後", reason="ワイワイ向き", url="https://venue.example/c"),
    ]
    return all_candidates[:count]


def test_three_candidates_render_as_character_reply_not_urls_only():
    candidates = venue_candidates()
    generated = (
        "このへん良さそう🍖\n① 焼肉A\n駅近で集まりやすい\nhttps://venue.example/a\n"
        "② 焼肉B\n少し落ち着いた感じ\nhttps://venue.example/b\n"
        "③ 焼肉C\nワイワイ向き\nhttps://venue.example/c\n俺なら①かな。"
    )
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RenderOpenAI([generated])

    reply = ai.render_venue_reply(VenueSearchCriteria(location="横浜"), candidates, "店探して")

    assert reply == generated
    assert "このへん良さそう" in reply
    assert not reply.startswith("http")
    assert all(candidate.url in reply for candidate in candidates)


def test_one_candidate_reply_has_name_description_and_exact_url():
    candidates = venue_candidates(1)
    generated = "今のところここがよさそう。\n焼肉A\n駅近で集まりやすい。\nhttps://venue.example/a\nもう少し広げる？"
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RenderOpenAI([generated])

    reply = ai.render_venue_reply(VenueSearchCriteria(location="横浜"), candidates, "店探して")

    assert "焼肉A" in reply
    assert "駅近" in reply
    assert candidates[0].url in reply


def test_zero_candidates_reply_is_natural_japanese_without_url():
    generated = "今の条件だと、これって店がうまく拾えなかった🙏\n駅を少し広げて探してみる？"
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RenderOpenAI([generated])

    reply = ai.render_venue_reply(VenueSearchCriteria(location="横浜"), [], "店探して")

    assert "拾えなかった" in reply
    assert "http" not in reply
    assert "0件" not in reply


def test_changed_or_invented_url_is_rejected_and_fallback_keeps_exact_urls():
    candidates = venue_candidates(1)
    generated = "焼肉Aはここ！\nhttps://fake.example/invented"
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RenderOpenAI([generated])

    reply = ai.render_venue_reply(VenueSearchCriteria(location="横浜"), candidates, "店探して")

    assert "https://fake.example/invented" not in reply
    assert candidates[0].url in reply
    assert "焼肉A" in reply
    assert len(reply.replace(candidates[0].url, "").strip()) > 20


def test_url_only_generated_reply_is_rejected():
    candidates = venue_candidates()
    generated = "\n".join(candidate.url for candidate in candidates)
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RenderOpenAI([generated])

    reply = ai.render_venue_reply(VenueSearchCriteria(location="横浜"), candidates, "店探して")

    assert reply != generated
    assert "このへん良さそう" in reply
    assert all(candidate.name in reply and candidate.url in reply for candidate in candidates)


def test_reply_generation_error_returns_character_fallback_not_exception_data():
    candidates = venue_candidates(1)
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = RenderOpenAI([RuntimeError('{"internal":"raw error"}')])

    reply = ai.render_venue_reply(VenueSearchCriteria(location="横浜"), candidates, "店探して")

    assert "raw error" not in reply
    assert "焼肉A" in reply
    assert candidates[0].url in reply


def test_normal_and_venue_reply_prompts_share_one_persona_constant():
    assert KANJI_PROMPT.startswith(BOT_PERSONA)
    assert VENUE_REPLY_PROMPT.startswith(BOT_PERSONA)
