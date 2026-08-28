from app.ai.budget import ApiBudget
from app.ai.conversation import ConversationAIClient
from app.models import ConversationResearch, ResearchSource
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
