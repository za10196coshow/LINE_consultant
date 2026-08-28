from app.ai.client import AIClient
from app.models import Action


class BrokenResponses:
    def parse(self, **kwargs):
        raise TimeoutError("timeout")


class BrokenOpenAI:
    responses = BrokenResponses()


def test_openai_exception_returns_ignore():
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo")
    ai.client = BrokenOpenAI()
    assert ai.decide("雑談", "A", {}).action == Action.IGNORE

