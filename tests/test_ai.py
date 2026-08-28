from app.ai.client import AIClient
from app.models import Action, Decision


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
