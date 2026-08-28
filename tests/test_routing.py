import pytest

from app.models import MessageRoute
from app.services.routing import MessageRouter


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("9月に飲もう", MessageRoute.ORGANIZER),
        ("俺19日行ける", MessageRoute.ORGANIZER),
        ("店探して", MessageRoute.ORGANIZER),
        ("iPhoneでPDFにする方法誰か知ってる？", MessageRoute.CONVERSATION_ASSISTANT),
        ("明日の天気どう？", MessageRoute.CONVERSATION_ASSISTANT),
        ("11時じゃない？", MessageRoute.CONVERSATION_ASSISTANT),
        ("ありがとう", MessageRoute.NO_ACTION),
        ("今日めっちゃ眠い", MessageRoute.NO_ACTION),
        ("お腹すいたなー", MessageRoute.CONVERSATION_ASSISTANT),
        ("明日の天気なんだろ", MessageRoute.CONVERSATION_ASSISTANT),
        ("遅刻しそう", MessageRoute.CONVERSATION_ASSISTANT),
        ("スマホの充電やばい", MessageRoute.CONVERSATION_ASSISTANT),
        ("眠いけどこれから運転", MessageRoute.CONVERSATION_ASSISTANT),
        ("笑", MessageRoute.NO_ACTION),
    ],
)
def test_message_routing(message, expected):
    assert MessageRouter().route(message) == expected


def test_open_issue_routes_followup_to_conversation_assistant():
    assert MessageRouter().route("それなら共有からできるよ", has_open_issues=True) == MessageRoute.CONVERSATION_ASSISTANT


def test_active_event_routes_event_preference_to_organizer():
    assert MessageRouter().route("横浜がいい", has_active_event=True) == MessageRoute.ORGANIZER
