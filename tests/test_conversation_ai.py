import pytest

from app.ai.budget import ApiBudget, ApiBudgetExceeded
from app.ai.conversation import ConversationAIClient
from app.models import ConversationAction, ConversationDecision, ConversationResearch, HelpLevel, HelpType, ResearchSource
from app.prompts.system import BOT_PERSONA, CONVERSATION_ASSISTANT_PROMPT, CONVERSATION_RESEARCH_REPLY_PROMPT
from app.repositories.database import Database


class ParsedResearchResponse:
    output_parsed = ConversationResearch(
        answer_summary="明日は雨の見込み",
        sources=[
            ResearchSource(title="公式天気", note="傘が必要", url="https://weather.example/official"),
            ResearchSource(title="未確認", note="不明", url="https://fake.example/weather"),
        ],
    )

    def model_dump(self, mode="json"):
        return {
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"sources": [{"url": "https://weather.example/official"}]},
                }
            ],
        }


class RecordingResponses:
    def __init__(self, parsed=None, text=None):
        self.parsed = parsed
        self.text = text
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.parsed

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return TextResponse(self.text)


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = responses


class TextResponse:
    def __init__(self, text):
        self.output_text = text

    def model_dump(self, mode="json"):
        return {"usage": {"input_tokens": 50, "output_tokens": 20}, "output": []}


def make_ai():
    db = Database(":memory:")
    return ConversationAIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo", ApiBudget(db, 100, 90, 150))


def test_conversation_web_search_keeps_only_verified_source_urls():
    ai = make_ai()
    responses = RecordingResponses(parsed=ParsedResearchResponse())
    ai.search_client = FakeOpenAI(responses)

    result = ai.research("明日の天気どう？", {"recent_messages": []})

    assert result is not None
    assert [source.url for source in result.sources] == ["https://weather.example/official"]
    assert responses.calls[0]["tools"][0]["type"] == "web_search"
    assert responses.calls[0]["tool_choice"] == "required"


def test_url_only_or_changed_url_reply_uses_safe_character_fallback():
    ai = make_ai()
    responses = RecordingResponses(text="https://fake.example/weather")
    ai.client = FakeOpenAI(responses)
    research = ConversationResearch(
        answer_summary="明日は雨の見込み https://invented.example/",
        sources=[ResearchSource(title="公式天気", note="傘が必要", url="https://weather.example/official")],
    )

    reply = ai.render_research_reply("明日の天気どう？", research)

    assert "明日は雨の見込み" in reply
    assert "傘が必要" in reply
    assert "https://weather.example/official" in reply
    assert "fake.example" not in reply
    assert "invented.example" not in reply


def test_conversation_prompts_share_the_common_persona():
    assert CONVERSATION_ASSISTANT_PROMPT.startswith(BOT_PERSONA)
    assert CONVERSATION_RESEARCH_REPLY_PROMPT.startswith(BOT_PERSONA)


def test_proactive_decision_schema_keeps_helpfulness_and_search_intent():
    decision = ConversationDecision(
        action=ConversationAction.PROACTIVE_HELP,
        reply_required=True,
        confidence=0.74,
        expected_helpfulness=0.91,
        intrusiveness_risk=0.18,
        help_type=HelpType.WEATHER,
        help_level=HelpLevel.WEB_RESEARCH,
        latent_need="明日の天気を知って外出準備を判断したい",
        need_confidence=0.87,
        urgency=0.45,
        actionability=0.95,
        information_needed=["location", "weather_forecast"],
        external_research_needed=True,
        suggested_action="天気を調べて傘や服装を短く助言する",
        need_category="weather",
        explicit_help_request=False,
        discomfort_signal=0.2,
        friction_signal=0.1,
    )

    assert decision.expected_helpfulness == 0.91
    assert decision.intrusiveness_risk == 0.18
    assert decision.web_search_required is True
    assert decision.latent_need == "明日の天気を知って外出準備を判断したい"
    assert decision.need_confidence == 0.87
    assert decision.information_needed == ["location", "weather_forecast"]


def test_high_implicit_discomfort_can_correct_no_action_without_explicit_request():
    decision = ConversationDecision(
        action=ConversationAction.NO_ACTION,
        reply_text="大丈夫？ 無理せず少し休んだ方がいいかも。",
        latent_need="腹痛への軽い対処が必要そう",
        confidence=0.82,
        need_confidence=0.82,
        discomfort_signal=0.91,
        friction_signal=0.35,
        expected_helpfulness=0.8,
        intrusiveness_risk=0.18,
        actionability=0.76,
    )

    assert decision.action == ConversationAction.PROACTIVE_HELP
    assert decision.reply_required is True
    assert decision.explicit_help_request is False


def test_blocking_missing_information_overrides_web_search_with_clarification():
    decision = ConversationDecision(
        action=ConversationAction.PROACTIVE_HELP,
        reply_required=True,
        confidence=0.85,
        expected_helpfulness=0.85,
        intrusiveness_risk=0.15,
        latent_need="現在地から目的地への行き方を知りたい",
        need_confidence=0.85,
        actionability=0.8,
        user_goal="品川駅への行き方を知る",
        known_facts=["目的地は品川駅"],
        missing_information=["current_location"],
        blocking_missing_information=["current_location"],
        can_answer_without_clarification=False,
        clarification_question="今どこにいる？",
        external_research_needed=True,
        web_search_required=True,
    )

    assert decision.action == ConversationAction.ASK_CLARIFICATION
    assert decision.research_ready is False
    assert decision.web_search_required is False
    assert decision.reply_text == "今どこにいる？"


def test_budget_limit_blocks_proactive_analysis_before_openai_call():
    db = Database(":memory:")
    budget = ApiBudget(db, 100, 90, 150)
    db.add_api_usage(budget.date_jst, model="gpt-5-mini", cost_jpy=90)
    ai = ConversationAIClient("test", "gpt-5-mini", "幹事", "Asia/Tokyo", budget)
    responses = RecordingResponses(parsed=None)
    ai.client = FakeOpenAI(responses)

    with pytest.raises(ApiBudgetExceeded):
        ai.analyze("明日の天気なんだろ", "U1", {"recent_messages": []})

    assert responses.calls == []
