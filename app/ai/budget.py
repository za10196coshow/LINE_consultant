import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.repositories.database import Database

logger = logging.getLogger(__name__)


class ApiBudgetExceeded(Exception):
    """Raised before an OpenAI request when the JST daily stop line is reached."""


@dataclass(frozen=True)
class ModelPrice:
    input_per_million_usd: float
    cached_input_per_million_usd: float
    output_per_million_usd: float


# OpenAI pricing checked 2026-08-29. Keep model pricing in this one table.
MODEL_PRICES: dict[str, ModelPrice] = {
    "gpt-5-mini": ModelPrice(0.25, 0.025, 2.00),
    "gpt-5-mini-2025-08-07": ModelPrice(0.25, 0.025, 2.00),
}
WEB_SEARCH_USD_PER_CALL = 0.01


@dataclass(frozen=True)
class UsageAmounts:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    web_search_count: int = 0


class ApiBudget:
    """Serializes guarded OpenAI calls and persists their estimated daily cost."""

    def __init__(
        self,
        db: Database,
        budget_jpy: float,
        stop_threshold_jpy: float,
        usd_jpy_rate: float,
        timezone: str = "Asia/Tokyo",
        now: Callable[[], datetime] | None = None,
    ):
        if budget_jpy <= 0 or stop_threshold_jpy <= 0 or usd_jpy_rate <= 0:
            raise ValueError("API budget settings must be positive")
        if stop_threshold_jpy > budget_jpy:
            raise ValueError("DAILY_API_STOP_THRESHOLD_JPY must not exceed DAILY_API_BUDGET_JPY")
        self.db = db
        self.budget_jpy = budget_jpy
        self.stop_threshold_jpy = stop_threshold_jpy
        self.usd_jpy_rate = usd_jpy_rate
        self.timezone = ZoneInfo(timezone)
        self._now = now or (lambda: datetime.now(self.timezone))
        self._call_lock = threading.RLock()

    @property
    def date_jst(self) -> str:
        return self._now().astimezone(self.timezone).date().isoformat()

    def status(self) -> dict[str, Any]:
        row = self.db.daily_api_usage(self.date_jst)
        daily_cost_jpy = float(row["cost_jpy"])
        return {
            **row,
            "date_jst": self.date_jst,
            "budget_jpy": self.budget_jpy,
            "stop_threshold_jpy": self.stop_threshold_jpy,
            "remaining_jpy": max(self.stop_threshold_jpy - daily_cost_jpy, 0.0),
            "request_allowed": daily_cost_jpy < self.stop_threshold_jpy,
        }

    def can_call(self) -> bool:
        with self._call_lock:
            status = self.status()
            self._log_budget(status)
            return bool(status["request_allowed"])

    def execute(self, *, operation: str, model: str, request: Callable[[], Any]) -> Any:
        """Check, perform, and record one Responses API attempt under one lock."""
        with self._call_lock:
            if model not in MODEL_PRICES:
                logger.error("API_BUDGET model_price_missing model=%s request_blocked=true", model)
                raise ApiBudgetExceeded
            status = self.status()
            self._log_budget(status)
            if not status["request_allowed"]:
                logger.warning(
                    "API_BUDGET_LIMIT_REACHED date_jst=%s daily_cost_jpy=%.4f request_blocked=true operation=%s",
                    status["date_jst"],
                    status["cost_jpy"],
                    operation,
                )
                raise ApiBudgetExceeded

            try:
                response = request()
            except Exception:
                # The provider does not return usage for failed/timed-out attempts.
                self.db.add_api_usage(self.date_jst, model=model, request_count=1)
                raise

            usage = extract_usage(response)
            cost_usd = estimate_cost_usd(model, usage)
            cost_jpy = cost_usd * self.usd_jpy_rate
            updated = self.db.add_api_usage(
                self.date_jst,
                model=model,
                cost_usd=cost_usd,
                cost_jpy=cost_jpy,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                request_count=1,
                web_search_count=usage.web_search_count,
            )
            logger.info(
                "API_USAGE model=%s operation=%s input_tokens=%d output_tokens=%d cached_input_tokens=%d "
                "estimated_cost_jpy=%.4f daily_cost_jpy=%.4f daily_request_count=%d daily_input_tokens=%d "
                "daily_output_tokens=%d",
                model,
                operation,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_input_tokens,
                cost_jpy,
                updated["cost_jpy"],
                updated["request_count"],
                updated["input_tokens"],
                updated["output_tokens"],
            )
            if usage.web_search_count:
                logger.info(
                    "WEB_SEARCH_USAGE estimated_cost_jpy=%.4f daily_cost_jpy=%.4f web_search_count=%d",
                    cost_jpy,
                    updated["cost_jpy"],
                    updated["web_search_count"],
                )
            return response

    def _log_budget(self, status: dict[str, Any]) -> None:
        logger.info(
            "API_BUDGET date_jst=%s daily_cost_usd=%.6f daily_cost_jpy=%.4f stop_threshold_jpy=%.2f "
            "budget_jpy=%.2f remaining_jpy=%.4f request_count=%d input_tokens=%d output_tokens=%d "
            "cached_input_tokens=%d web_search_count=%d request_allowed=%s",
            status["date_jst"],
            status["cost_usd"],
            status["cost_jpy"],
            status["stop_threshold_jpy"],
            status["budget_jpy"],
            status["remaining_jpy"],
            status["request_count"],
            status["input_tokens"],
            status["output_tokens"],
            status["cached_input_tokens"],
            status["web_search_count"],
            str(status["request_allowed"]).lower(),
        )


def estimate_cost_usd(model: str, usage: UsageAmounts) -> float:
    price = MODEL_PRICES.get(model)
    if price is None:
        raise ValueError(f"No API price configured for model: {model}")
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    uncached = max(usage.input_tokens - cached, 0)
    return (
        uncached * price.input_per_million_usd / 1_000_000
        + cached * price.cached_input_per_million_usd / 1_000_000
        + usage.output_tokens * price.output_per_million_usd / 1_000_000
        + usage.web_search_count * WEB_SEARCH_USD_PER_CALL
    )


def extract_usage(response: Any) -> UsageAmounts:
    payload = response.model_dump(mode="json") if hasattr(response, "model_dump") else response if isinstance(response, dict) else {}
    raw = payload.get("usage") or {}
    details = raw.get("input_tokens_details") or {}
    output = payload.get("output") or []
    web_search_count = sum(1 for item in output if isinstance(item, dict) and item.get("type") == "web_search_call")
    return UsageAmounts(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cached_input_tokens=int(details.get("cached_tokens") or 0),
        web_search_count=web_search_count,
    )
