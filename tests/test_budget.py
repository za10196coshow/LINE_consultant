import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from openai import APITimeoutError

from app.ai.budget import ApiBudget, ApiBudgetExceeded, UsageAmounts, estimate_cost_usd
from app.ai.client import AIClient
from app.models import VenueCandidate, VenueSearchCriteria
from app.repositories.database import Database
from app.services.kanji import KanjiService


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


@pytest.mark.parametrize(
    ("daily_cost", "allowed"),
    [(0, True), (89, True), (89.99, True), (90, False), (95, False), (100, False), (150, False)],
)
def test_budget_threshold(daily_cost, allowed):
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)
    if daily_cost:
        db.add_api_usage(budget.date_jst, model="gpt-5-mini", cost_jpy=daily_cost)
    assert budget.can_call() is allowed
    assert budget.status()["remaining_jpy"] == max(90 - daily_cost, 0)


def test_jst_midnight_starts_new_daily_bucket_before_utc_date_changes():
    clock = Clock(datetime(2026, 8, 29, 14, 59, tzinfo=ZoneInfo("UTC")))
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150, now=clock)
    db.add_api_usage("2026-08-29", model="gpt-5-mini", cost_jpy=90)
    assert not budget.can_call()

    clock.value = datetime(2026, 8, 29, 15, 0, tzinfo=ZoneInfo("UTC"))
    assert budget.date_jst == "2026-08-30"
    assert budget.can_call()


def test_daily_usage_survives_database_and_budget_recreation(tmp_path):
    path = tmp_path / "usage.db"
    first_db = Database(str(path))
    first = ApiBudget(first_db, 100, 90, 150)
    first_db.add_api_usage(first.date_jst, model="gpt-5-mini", cost_jpy=91, request_count=4)

    restarted = ApiBudget(Database(str(path)), 100, 90, 150)
    assert not restarted.can_call()
    assert restarted.status()["request_count"] == 4


def test_cost_calculation_includes_cached_tokens_and_web_search_fee():
    usage = UsageAmounts(input_tokens=1000, cached_input_tokens=200, output_tokens=100, web_search_count=1)
    assert estimate_cost_usd("gpt-5-mini", usage) == pytest.approx(0.010405)


class UsageResponse:
    def model_dump(self, mode="json"):
        return {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 200},
            },
            "output": [{"type": "web_search_call"}],
        }


def test_successful_call_persists_usage_request_and_web_search_counts():
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)

    assert budget.execute(operation="web_search", model="gpt-5-mini", request=UsageResponse) is not None

    status = budget.status()
    assert status["cost_usd"] == pytest.approx(0.010405)
    assert status["cost_jpy"] == pytest.approx(1.56075)
    assert status["input_tokens"] == 1000
    assert status["cached_input_tokens"] == 200
    assert status["output_tokens"] == 100
    assert status["request_count"] == 1
    assert status["web_search_count"] == 1
    assert status["models"] == "gpt-5-mini"


class NeverResponses:
    def __init__(self):
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        raise AssertionError("OpenAI must not be called")

    def create(self, **kwargs):
        self.calls += 1
        raise AssertionError("OpenAI must not be called")


class NeverOpenAI:
    def __init__(self):
        self.responses = NeverResponses()


def blocked_ai():
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)
    db.add_api_usage(budget.date_jst, model="gpt-5-mini", cost_jpy=90)
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo", budget=budget)
    ai.client = NeverOpenAI()
    ai.search_client = NeverOpenAI()
    return ai


def test_limit_blocks_decision_without_openai_call():
    ai = blocked_ai()
    with pytest.raises(ApiBudgetExceeded):
        ai.decide("今どんな感じ？", "A", {})
    assert ai.client.responses.calls == 0


def test_limit_blocks_web_search_without_openai_call():
    ai = blocked_ai()
    with pytest.raises(ApiBudgetExceeded):
        ai.search(VenueSearchCriteria(location="横浜"), "店探して")
    assert ai.search_client.responses.calls == 0


def test_limit_blocks_search_reply_generation_without_openai_call():
    ai = blocked_ai()
    candidates = [VenueCandidate(name="店A", reason="駅近", url="https://example.com/a")]
    with pytest.raises(ApiBudgetExceeded):
        ai.render_venue_reply(VenueSearchCriteria(location="横浜"), candidates, "店探して")
    assert ai.client.responses.calls == 0


def timeout_error():
    return APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


class ThresholdOnTimeoutResponses:
    def __init__(self, budget):
        self.calls = 0
        self.budget = budget

    def parse(self, **kwargs):
        self.calls += 1
        self.budget.db.add_api_usage(self.budget.date_jst, model="gpt-5-mini", cost_jpy=90)
        raise timeout_error()


class ThresholdOnTimeoutOpenAI:
    def __init__(self, budget):
        self.responses = ThresholdOnTimeoutResponses(budget)


def test_retry_checks_budget_again_and_does_not_call_twice():
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)
    ai = AIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo", budget=budget)
    ai.client = ThresholdOnTimeoutOpenAI(budget)

    with pytest.raises(ApiBudgetExceeded):
        ai.decide("今どんな感じ？", "A", {})
    assert ai.client.responses.calls == 1


class DummyAI:
    def __init__(self, budget):
        self.budget = budget


class DummyLine:
    def __init__(self):
        self.pushes = []

    def push(self, conversation_id, text):
        self.pushes.append((conversation_id, text))
        return True


def test_budget_notification_is_once_per_group_per_jst_day():
    clock = Clock(datetime(2026, 8, 29, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    db = Database(":memory:")
    line = DummyLine()
    service = KanjiService(db, DummyAI(ApiBudget(db, 100, 90, 150, now=clock)), line)

    service._notify_budget_limit("G1")
    service._notify_budget_limit("G1")
    service._notify_budget_limit("G2")
    assert [target for target, _ in line.pushes] == ["G1", "G2"]
    assert all("働きすぎた" in text for _, text in line.pushes)

    clock.value = datetime(2026, 8, 30, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    service._notify_budget_limit("G1")
    assert [target for target, _ in line.pushes] == ["G1", "G2", "G1"]


def test_unknown_model_is_blocked_before_request():
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)
    called = False

    def request():
        nonlocal called
        called = True

    with pytest.raises(ApiBudgetExceeded):
        budget.execute(operation="decision", model="unpriced-model", request=request)
    assert not called


def test_concurrent_calls_are_serialized_before_budget_check():
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)
    db.add_api_usage(budget.date_jst, model="gpt-5-mini", cost_jpy=89.9)
    entered = threading.Event()
    release = threading.Event()
    request_calls = 0

    def request():
        nonlocal request_calls
        request_calls += 1
        entered.set()
        release.wait(timeout=2)
        return UsageResponse()

    def guarded_call():
        try:
            budget.execute(operation="web_search", model="gpt-5-mini", request=request)
            return "called"
        except ApiBudgetExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(guarded_call)
        assert entered.wait(timeout=1)
        second = pool.submit(guarded_call)
        release.set()
        results = {first.result(timeout=2), second.result(timeout=2)}

    assert results == {"called", "blocked"}
    assert request_calls == 1
