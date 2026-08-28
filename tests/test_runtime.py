import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_test_runtime_is_python_312():
    assert sys.version_info[:2] == (3, 12)


def test_render_and_repository_pin_python_312():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    render = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    env = {item["key"]: str(item.get("value", "")) for item in render["services"][0]["envVars"]}
    assert pinned == "3.12.7"
    assert env["PYTHON_VERSION"] == pinned
    assert 'requires-python = ">=3.12,<3.13"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_render_has_daily_api_budget_defaults():
    render = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    env = {item["key"]: str(item.get("value", "")) for item in render["services"][0]["envVars"]}
    assert env["DAILY_API_BUDGET_JPY"] == "100"
    assert env["DAILY_API_STOP_THRESHOLD_JPY"] == "90"
    assert env["USD_JPY_RATE"] == "150"


def test_render_has_conversation_assistant_defaults():
    render = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    env = {item["key"]: str(item.get("value", "")) for item in render["services"][0]["envVars"]}
    assert env["CONVERSATION_ASSISTANT_COOLDOWN_MINUTES"] == "20"
    assert env["UNANSWERED_QUESTION_DELAY_SECONDS"] == "30"
    assert env["UNANSWERED_QUESTION_DELAY_MESSAGES"] == "1"
    assert env["CONVERSATION_ASSISTANT_CONFIDENCE_THRESHOLD"] == "0.78"
    assert env["CONVERSATION_PROACTIVE_THRESHOLD"] == "0.65"
