from datetime import datetime, timedelta, timezone

import pytest

from app.ai.budget import ApiBudget, ApiBudgetExceeded
from app.models import (
    ConversationAction,
    ConversationDecision,
    ConversationResearch,
    Decision,
    HelpLevel,
    HelpType,
    PreferenceUpdate,
    ResearchSource,
)
from app.repositories.database import Database
from app.services.conversation_assistant import ConversationAssistant


def event(message_id, text, user_id="U1"):
    return {
        "type": "message",
        "webhookEventId": f"evt-{message_id}",
        "timestamp": 1788000000000,
        "source": {"type": "group", "groupId": "G1", "userId": user_id},
        "message": {"id": message_id, "type": "text", "text": text},
    }


class FakeLine:
    def __init__(self):
        self.pushes = []

    def display_name(self, user_id, *_):
        return user_id

    def push(self, target, text):
        self.pushes.append((target, text))
        return True


class QueueAI:
    def __init__(self, db, decisions):
        self.db = db
        self.decisions = list(decisions)
        self.calls = []
        self.budget = ApiBudget(db, 100, 90, 150)
        self.research_result = None
        self.research_calls = []
        self.rendered = None

    def analyze(self, message, display_name, context):
        self.calls.append((message, display_name, context))
        item = self.decisions.pop(0)
        return item(context) if callable(item) else item

    def research(self, message, context):
        self.research_calls.append((message, context))
        return self.research_result

    def render_research_reply(self, message, research):
        return self.rendered


def issue_decision(summary="PDFへの変換方法が未解決", reply="共有メニューからPDFにできるよ。"):
    return ConversationDecision(
        action=ConversationAction.UNANSWERED_QUESTION,
        reply_required=True,
        confidence=0.92,
        reason=summary,
        reply_text=reply,
        topic="iPhone",
        issue_type="UNANSWERED_QUESTION",
        summary=summary,
    )


def proactive_decision(
    help_type,
    summary,
    reply,
    *,
    web=False,
    confidence=0.76,
    helpfulness=0.9,
    risk=0.2,
    level=HelpLevel.ADVICE,
    discomfort=0.0,
    friction=0.0,
    explicit=False,
):
    return ConversationDecision(
        action=ConversationAction.PROACTIVE_HELP,
        reply_required=True,
        confidence=confidence,
        expected_helpfulness=helpfulness,
        intrusiveness_risk=risk,
        help_type=help_type,
        help_level=level,
        reason=summary,
        reply_text=reply,
        topic=help_type.value,
        issue_type="POTENTIAL_NEED",
        summary=summary,
        web_search_required=web,
        latent_need=summary,
        need_confidence=confidence,
        urgency=0.4,
        actionability=helpfulness,
        external_research_needed=web,
        suggested_action=reply or "必要な情報を確認して短く助言する",
        need_category=help_type.value,
        explicit_help_request=explicit,
        discomfort_signal=discomfort,
        friction_signal=friction,
    )


def clarification_decision(goal, latent_need, question, blocking, *, partial_reply=None):
    return ConversationDecision(
        action=ConversationAction.ASK_CLARIFICATION,
        reply_required=True,
        confidence=0.88,
        expected_helpfulness=0.86,
        intrusiveness_risk=0.12,
        latent_need=latent_need,
        need_confidence=0.86,
        urgency=0.45,
        actionability=0.82,
        friction_signal=0.8,
        user_goal=goal,
        known_facts=["目的地候補がある"],
        missing_information=list(blocking),
        blocking_missing_information=list(blocking),
        can_answer_without_clarification=False,
        clarification_question=question,
        reply_text=partial_reply or question,
        top_intent_confidence=0.62,
        research_ready=False,
        external_research_needed=True,
        web_search_required=True,
        suggested_action="最重要の不足情報を一つ確認する",
        need_category="navigation",
        help_type=HelpType.NAVIGATION,
        help_level=HelpLevel.LIGHT,
    )


def follow_up_decision(reply, *, confidence=0.95, known_facts=None, pending_question=None, close=False):
    return ConversationDecision(
        action=ConversationAction.FOLLOW_UP,
        reply_required=not close or bool(reply),
        continuation_confidence=confidence,
        resolved_reference="直前のAI質問への回答",
        reply_text=reply,
        topic_summary="すぐ作れる食事を相談中",
        user_goal="短時間で食事を作りたい",
        known_facts=known_facts or [],
        open_questions=[pending_question] if pending_question else [],
        pending_question=pending_question,
        pending_question_type="COOKING_INGREDIENTS" if pending_question else None,
        expected_response_types=["材料", "調理時間"] if pending_question else [],
        close_topic=close,
    )


def make_service(decisions, *, delay_messages=1, cooldown=20):
    db = Database(":memory:")
    line = FakeLine()
    ai = QueueAI(db, decisions)
    service = ConversationAssistant(
        db,
        ai,
        line,
        "幹事",
        cooldown_minutes=cooldown,
        unanswered_delay_seconds=3600,
        unanswered_delay_messages=delay_messages,
    )
    return service, db, ai, line


def test_question_then_human_answer_resolves_without_ai_intervention():
    def resolved(context):
        return ConversationDecision(
            action=ConversationAction.NO_ACTION,
            resolves_issue_id=context["open_issues"][0]["id"],
        )

    service, db, _, line = make_service([issue_decision(), resolved])
    service.handle(event("q1", "iPhoneでPDFにする方法誰か知ってる？"))
    service.handle(event("a1", "共有メニューからプリントを選べばできるよ", "U2"))

    assert line.pushes == []
    assert db.open_conversation_issues("G1") == []


def test_unanswered_question_intervenes_after_one_followup_message():
    decision = issue_decision()
    service, db, _, line = make_service([decision, decision])
    service.handle(event("q1", "iPhoneでPDFにする方法誰か知ってる？"))
    assert line.pushes == []

    service.handle(event("m2", "うーん", "U2"))
    assert line.pushes == [("G1", "共有メニューからPDFにできるよ。")]
    assert db.open_conversation_issues("G1")[0]["last_notified_at"] is not None


def test_conflict_reply_uses_prior_speaker_messages():
    conflict = ConversationDecision(
        action=ConversationAction.CLARIFY_CONFLICT,
        reply_required=True,
        confidence=0.95,
        reason="集合時刻が10時と11時で異なる",
        reply_text="そこ、10時と11時でズレてるっぽい。どっちが確定だっけ？",
        topic="集合時刻",
        issue_type="CONFLICT",
        summary="集合時刻が10時と11時で異なる",
    )
    service, _, ai, line = make_service([conflict], delay_messages=0)
    service.db.ensure_group("G1", "group")
    service.db.add_message("G1", None, "U1", "A", "集合10時", "m0", 1)

    service.handle(event("m1", "11時じゃない？", "U2"))

    assert ai.calls[0][2]["recent_messages"][0]["message_text"] == "集合10時"
    assert "10時と11時" in line.pushes[0][1]


def test_cooldown_skips_new_issue_but_explicit_call_bypasses_it():
    first = issue_decision("最初の課題", "最初の回答")
    second = issue_decision("別の課題", "二つ目の回答")
    third = issue_decision("明示質問", "呼ばれたので回答")
    service, _, _, line = make_service([first, second, third], delay_messages=0)

    service.handle(event("m1", "これどうすればいい？"))
    service.handle(event("m2", "別件も分からない", "U2"))
    service.handle(event("m3", "AI、これ教えて", "U2"))

    assert [text for _, text in line.pushes] == ["最初の回答", "呼ばれたので回答"]


def test_same_issue_is_not_notified_twice():
    decision = issue_decision()
    service, _, _, line = make_service([decision, decision], delay_messages=0, cooldown=0)
    service.handle(event("m1", "これ分からない"))
    service.handle(event("m2", "まだ分からない", "U2"))
    assert len(line.pushes) == 1


def test_resolved_issue_is_not_reopened_or_repeated():
    decision = issue_decision()

    def resolved(context):
        return ConversationDecision(action=ConversationAction.NO_ACTION, resolves_issue_id=context["open_issues"][0]["id"])

    service, db, _, line = make_service([decision, resolved, decision], delay_messages=0, cooldown=0)
    service.handle(event("m1", "これ分からない"))
    service.handle(event("m2", "こうすればできるよ", "U2"))
    service.handle(event("m3", "やっぱりこれ分からない"))

    assert len(line.pushes) == 1
    assert db.open_conversation_issues("G1") == []


def test_web_research_uses_character_rendered_reply_not_url_only():
    decision = ConversationDecision(
        action=ConversationAction.WEB_RESEARCH,
        reply_required=True,
        confidence=0.95,
        reason="明日の天気は最新情報が必要",
        topic="天気",
        issue_type="CURRENT_INFORMATION",
        summary="東京の明日の天気",
    )
    service, _, ai, line = make_service([decision], delay_messages=0)
    ai.research_result = ConversationResearch(
        answer_summary="明日は雨の見込み",
        sources=[ResearchSource(title="天気予報", note="傘が必要そう", url="https://weather.example/tokyo")],
    )
    ai.rendered = "明日は雨っぽいから傘あった方がよさそう。\nhttps://weather.example/tokyo"

    service.handle(event("m1", "明日の天気どう？"))

    assert line.pushes[0][1].startswith("明日は雨っぽい")
    assert line.pushes[0][1] != "https://weather.example/tokyo"


def test_budget_limit_uses_fixed_message_once():
    service, db, ai, line = make_service([], delay_messages=0)
    db.add_api_usage(ai.budget.date_jst, model="gpt-5-mini", cost_jpy=90)

    def blocked(*_):
        raise ApiBudgetExceeded

    ai.analyze = blocked
    service.handle_safely(event("m1", "AI、教えて"))
    service.handle_safely(event("m2", "AI、もう一回教えて"))

    assert line.pushes == [("G1", "今日はAI幹事ちょっと働きすぎたので、続きは明日で🙏")]


def test_food_need_with_known_location_uses_context_and_character_research_reply():
    decision = proactive_decision(
        HelpType.FOOD,
        "横浜駅周辺で食事が必要",
        None,
        web=True,
        level=HelpLevel.WEB_RESEARCH,
    )
    service, db, ai, line = make_service([decision])
    db.ensure_group("G1", "group")
    db.add_message("G1", None, "U2", "U2", "横浜駅に着いた", "prior", 1)
    ai.research_result = ConversationResearch(answer_summary="近くに飲食店あり", sources=[])
    ai.rendered = "腹減ったなら、駅近くでサクッと食べられるところ見つけたよ。"

    service.handle(event("food1", "お腹すいたなー"))

    assert ai.calls[0][2]["recent_messages"][0]["message_text"] == "横浜駅に着いた"
    assert ai.research_result is not None
    assert line.pushes[0][1].startswith("腹減ったなら")


def test_food_need_without_location_can_offer_light_help_without_search():
    decision = proactive_decision(HelpType.FOOD, "食事を探したそう", "近くでなんか探す？ 今いる場所分かればすぐ見れるよ。")
    service, _, ai, line = make_service([decision])

    service.handle(event("food2", "お腹すいたなー"))

    assert ai.research_result is None
    assert "今いる場所" in line.pushes[0][1]


def test_implicit_weather_need_searches_and_adds_action_advice():
    decision = proactive_decision(
        HelpType.WEATHER,
        "明日の天気を気にしている",
        None,
        web=True,
        level=HelpLevel.WEB_RESEARCH,
    )
    service, _, ai, line = make_service([decision])
    ai.research_result = ConversationResearch(answer_summary="午後から雨", sources=[])
    ai.rendered = "明日は午後から雨っぽい。折りたたみ持っといた方がよさそう☔"

    service.handle(event("weather1", "明日の天気なんだろ"))

    assert "折りたたみ" in line.pushes[0][1]


def test_delay_and_battery_needs_get_practical_advice():
    delay = proactive_decision(HelpType.DELAY, "集合に遅れそう", "先に10分くらい遅れそうって入れとくのがよさそう。")
    battery = proactive_decision(HelpType.BATTERY, "充電が少ない", "低電力モード入れて、画面暗めにしとくと少し持つよ。")
    service, _, _, line = make_service([delay, battery], cooldown=0)

    service.handle(event("delay1", "遅刻しそう"))
    service.handle(event("battery1", "スマホの充電やばい", "U2"))

    assert "先に" in line.pushes[0][1]
    assert "低電力モード" in line.pushes[1][1]


def test_low_helpfulness_proactive_need_stays_silent_and_does_not_open_issue():
    decision = proactive_decision(HelpType.ACTIVITY, "ただ眠い", "休んだ方がいいよ", helpfulness=0.4)
    service, db, _, line = make_service([decision])

    service.handle(event("sleep1", "眠い"))

    assert line.pushes == []
    assert db.open_conversation_issues("G1") == []


def test_safety_need_bypasses_cooldown():
    first = proactive_decision(HelpType.FOOD, "食事が必要", "近くで探す？")
    safety = proactive_decision(
        HelpType.SAFETY,
        "眠い状態で運転予定",
        "その状態で運転は危ない。いったん安全な場所で休んで、運転を代われるなら代わってもらおう。",
        helpfulness=0.99,
        risk=0.05,
        level=HelpLevel.ACTIVE_SUPPORT,
    )
    service, _, _, line = make_service([first, safety])

    service.handle(event("food3", "腹減った"))
    service.handle(event("safety1", "眠いけどこれから運転", "U2"))

    assert len(line.pushes) == 2
    assert "運転は危ない" in line.pushes[1][1]


def test_same_proactive_food_issue_is_not_repeated():
    decision = proactive_decision(HelpType.FOOD, "横浜駅で食事が必要", "駅近くで探す？")
    service, _, _, line = make_service([decision, decision], cooldown=0)

    service.handle(event("food4", "腹減った"))
    service.handle(event("food5", "俺も腹減った", "U2"))

    assert len(line.pushes) == 1


def test_different_topic_can_bypass_cooldown():
    food = proactive_decision(HelpType.FOOD, "食事が必要", "近くで探す？", helpfulness=0.75, risk=0.3)
    battery = proactive_decision(HelpType.BATTERY, "充電が少ない", "低電力モード入れとくといいよ。", helpfulness=0.75, risk=0.3)
    service, _, _, line = make_service([food, battery])

    service.handle(event("food6", "腹減った"))
    service.handle(event("battery2", "充電やばい", "U2"))

    assert len(line.pushes) == 2


def test_natural_weather_monologue_with_context_location_forces_web_search_and_character_reply():
    weather = proactive_decision(
        HelpType.WEATHER,
        "明日の天気を知って外出準備を判断したい",
        None,
        web=True,
        level=HelpLevel.WEB_RESEARCH,
    )
    service, db, ai, line = make_service([weather])
    db.ensure_group("G1", "group")
    db.add_message("G1", None, "U2", "U2", "横浜駅に着いた", "place1", 1)
    ai.research_result = ConversationResearch(answer_summary="午後から雨", sources=[])
    ai.rendered = "明日の横浜、午後ちょっと雨っぽいよ。折りたたみ持っといた方がよさそう☔"

    service.handle(event("weather-natural", "明日天気何かなー？"))

    assert ai.calls[0][2]["recent_messages"][0]["message_text"] == "横浜駅に着いた"
    assert ai.research_calls[0][1]["latent_need"] == "明日の天気を知って外出準備を判断したい"
    assert line.pushes == [("G1", ai.rendered)]
    issue = db.open_conversation_issues("G1")[0]
    assert issue["issue_type"] == "POTENTIAL_NEED"
    assert "外出準備" in issue["summary"]


def test_natural_weather_monologue_without_location_asks_naturally_without_search():
    weather = proactive_decision(
        HelpType.WEATHER,
        "明日の天気を知りたいが場所が不明",
        "どこの天気？ 場所わかれば見てみるよ。",
    )
    service, db, ai, line = make_service([weather])

    service.handle(event("weather-unknown", "明日天気何かなー"))

    assert ai.research_calls == []
    assert line.pushes == [("G1", "どこの天気？ 場所わかれば見てみるよ。")]
    assert db.open_conversation_issues("G1")[0]["issue_type"] == "POTENTIAL_NEED"


def test_weather_location_is_resolved_from_current_message_first():
    weather = proactive_decision(
        HelpType.WEATHER,
        "横浜の明日の天気を知って外出準備を判断したい",
        None,
        web=True,
        level=HelpLevel.WEB_RESEARCH,
    )
    service, _, ai, line = make_service([weather])
    ai.research_result = ConversationResearch(answer_summary="晴れ", sources=[])
    ai.rendered = "明日の横浜は晴れそう。昼なら出かけやすそうだよ。"

    service.handle(event("weather-current", "明日の横浜の天気何かなー？"))

    assert ai.research_calls[0][0] == "明日の横浜の天気何かなー？"
    assert line.pushes


def test_weather_location_uses_saved_active_event_area():
    weather = proactive_decision(
        HelpType.WEATHER,
        "明日の天気を知って外出準備を判断したい",
        None,
        web=True,
        level=HelpLevel.WEB_RESEARCH,
    )
    service, db, ai, line = make_service([weather])
    db.ensure_group("G1", "group")
    event_id = db.create_event("G1", "週末のお出かけ")
    participant_id = db.ensure_participant(event_id, "U1", "U1")
    db.save_decision(
        event_id,
        participant_id,
        Decision(action="REMEMBER_ONLY", preference_update=PreferenceUpdate(area="横浜")),
    )
    ai.research_result = ConversationResearch(answer_summary="晴れ", sources=[])
    ai.rendered = "明日の横浜は晴れそう。昼なら動きやすそうだよ。"

    service.handle(event("weather-event", "明日天気何かなー？"))

    assert ai.calls[0][2]["event_context"]["preferences"]["area"] == "横浜"
    assert line.pushes


def test_new_weather_topic_bypasses_food_cooldown_but_same_weather_is_duplicate():
    food = proactive_decision(HelpType.FOOD, "食事が必要", "近くで探す？", helpfulness=0.75, risk=0.3)
    weather = proactive_decision(
        HelpType.WEATHER,
        "明日の横浜の天気を知りたい",
        None,
        web=True,
        level=HelpLevel.WEB_RESEARCH,
    )
    service, db, ai, line = make_service([food, weather, weather])
    db.ensure_group("G1", "group")
    db.add_message("G1", None, "U2", "U2", "横浜駅に着いた", "place2", 1)
    ai.research_result = ConversationResearch(answer_summary="雨", sources=[])
    ai.rendered = "明日の横浜は雨っぽい。傘持っとこう。"

    service.handle(event("food-before-weather", "腹減った"))
    service.handle(event("weather-first", "明日雨かな", "U2"))
    service.handle(event("weather-repeat", "明日天気どうだろ", "U2"))

    assert len(line.pushes) == 2
    assert len(ai.research_calls) == 1


@pytest.mark.parametrize(
    ("text", "latent_need", "reply"),
    [
        ("お腹すいたなー", "食事をしたいか、何を食べるか決めたい", "近くでなんか探す？ 今いる場所分かれば見れるよ。"),
        ("遅刻しそう", "移動・到着時刻・相手への連絡に困っている", "先に遅れそうって一言入れとくのがよさそう。"),
        ("充電ない", "端末を使い続けるためにバッテリーを延命したい", "低電力モードと画面暗めで少し延命できるよ。"),
        ("これ高すぎない？", "価格が妥当か確認し、安い代替案も知りたい", "相場と比べてみる？ 商品名分かれば近い候補も見れるよ。"),
        (
            "プレゼント何買ったらいいか全然決まらん",
            "相手に合うプレゼント選びを手伝ってほしい",
            "相手の年代と好きなもの分かれば、候補一緒に絞れるよ。",
        ),
    ],
)
def test_free_form_latent_needs_are_not_limited_to_fixed_categories(text, latent_need, reply):
    decision = proactive_decision(HelpType.OTHER, latent_need, reply, helpfulness=0.82, risk=0.2)
    decision.need_category = "free_form"
    service, db, _, line = make_service([decision], cooldown=0)

    service.handle(event(f"latent-{abs(hash(text))}", text))

    assert line.pushes == [("G1", reply)]
    assert db.open_conversation_issues("G1")[0]["summary"] == latent_need


def test_child_boredom_uses_outing_context_for_free_form_need():
    decision = proactive_decision(
        HelpType.OTHER,
        "外出先で子どもが楽しめる次の行動を見つけたい",
        "近くで子どもが遊べるところ探す？ 今いる渋谷周辺で見れるよ。",
        helpfulness=0.9,
        risk=0.15,
    )
    service, db, ai, line = make_service([decision])
    db.ensure_group("G1", "group")
    db.add_message("G1", None, "U2", "U2", "今渋谷に着いた", "outing-context", 1)

    service.handle(event("child-bored", "子ども飽きてきた"))

    assert ai.calls[0][2]["recent_messages"][0]["message_text"] == "今渋谷に着いた"
    assert line.pushes


@pytest.mark.parametrize("text", ["暇だな", "眠い"])
def test_weak_latent_need_can_be_analyzed_but_stay_no_action(text):
    service, db, _, line = make_service([ConversationDecision()])

    service.handle(event(f"weak-{text}", text))

    assert line.pushes == []
    assert db.open_conversation_issues("G1") == []


def test_intervention_score_combines_need_helpfulness_intrusiveness_urgency_and_actionability():
    decision = proactive_decision(
        HelpType.SAFETY,
        "眠い状態で運転する危険を避けたい",
        "そのまま運転は危ない。安全な場所でいったん休もう。",
        confidence=0.9,
        helpfulness=0.95,
        risk=0.05,
        level=HelpLevel.ACTIVE_SUPPORT,
    )
    decision.urgency = 1.0
    decision.actionability = 1.0
    decision.discomfort_signal = 0.9
    service, _, _, _ = make_service([decision])

    assert service._intervention_score(decision) > 0.8


def test_implicit_stomach_pain_gets_light_natural_help_without_web_search():
    decision = proactive_decision(
        HelpType.OTHER,
        "腹痛に対する軽い対処や安心材料が必要そう",
        "大丈夫？ とりあえず無理せずちょっと休んで、水分だけ取っといた方がいいかも。",
        confidence=0.82,
        helpfulness=0.80,
        risk=0.18,
        discomfort=0.91,
        friction=0.35,
        level=HelpLevel.ADVICE,
    )
    decision.urgency = 0.45
    decision.actionability = 0.76
    service, _, ai, line = make_service([decision])

    service.handle(event("stomach-pain", "お腹痛いなー"))

    assert decision.explicit_help_request is False
    assert ai.research_calls == []
    assert "休んで" in line.pushes[0][1]


def test_explicit_stomach_pain_request_gets_priority_answer():
    decision = proactive_decision(
        HelpType.OTHER,
        "腹痛への対処を知りたい",
        "まず無理せず休もう。痛みが強くなるなら我慢せず受診も考えて。",
        confidence=0.9,
        helpfulness=0.88,
        risk=0.12,
        discomfort=0.9,
        explicit=True,
    )
    service, _, _, line = make_service([decision])

    service.handle(event("stomach-explicit", "お腹が痛い、どうすればいい？"))

    assert line.pushes


@pytest.mark.parametrize(
    ("text", "latent_need", "reply", "discomfort", "friction"),
    [
        ("頭痛いな", "頭痛への軽い対処が必要そう", "ちょっと休んで、水分取っといた方がいいかも。", 0.85, 0.2),
        ("充電ない", "端末を使い続けるための支援が必要", "低電力モードと画面暗めで少し延命できるよ。", 0.2, 0.85),
        ("財布忘れた", "支払い手段や財布の回収方法を整理したい", "取りに戻れるか確認して、無理なら使える決済を先に見とこう。", 0.2, 0.9),
        ("遅刻しそう", "移動と遅刻連絡を助けてほしい", "先に遅れそうって一言入れとくのがよさそう。", 0.25, 0.85),
    ],
)
def test_discomfort_and_friction_generalize_beyond_health(text, latent_need, reply, discomfort, friction):
    decision = proactive_decision(
        HelpType.OTHER,
        latent_need,
        reply,
        confidence=0.8,
        helpfulness=0.78,
        risk=0.18,
        discomfort=discomfort,
        friction=friction,
    )
    service, _, _, line = make_service([decision], cooldown=0)

    service.handle(event(f"friction-{abs(hash(text))}", text))

    assert line.pushes == [("G1", reply)]


def test_ambiguous_station_location_asks_clarification_without_search_failure():
    decision = clarification_decision(
        "品川駅の所在地か、現在地からの行き方を知りたい",
        "品川駅へ行くための情報が必要",
        "品川駅そのものの場所？ それとも今いるところからの行き方？",
        ["question_intent_or_current_location"],
        partial_reply="品川駅自体は東京都港区にある駅だよ。今いる場所からの行き方が知りたい感じ？",
    )
    service, _, ai, line = make_service([decision])

    service.handle(event("station-ambiguous", "困った。品川駅がどこか分からない"))

    assert ai.research_calls == []
    assert "東京都港区" in line.pushes[0][1]
    assert "調べもの" not in line.pushes[0][1]


def test_recent_location_is_used_without_asking_where_again():
    decision = proactive_decision(
        HelpType.NAVIGATION,
        "東京駅から品川駅への行き方を知りたい",
        None,
        web=True,
        confidence=0.9,
        helpfulness=0.9,
        risk=0.1,
        friction=0.8,
        level=HelpLevel.WEB_RESEARCH,
    )
    decision.user_goal = "東京駅から品川駅へ移動する"
    decision.known_facts = ["現在地候補は東京駅", "目的地は品川駅"]
    decision.can_answer_without_clarification = True
    decision.research_ready = True
    service, db, ai, line = make_service([decision])
    db.ensure_group("G1", "group")
    db.add_message("G1", None, "U1", "U1", "今東京駅", "tokyo-now", 1)
    ai.research_result = ConversationResearch(answer_summary="山手線などで移動可能", sources=[])
    ai.rendered = "今東京駅なら、品川までは山手線で向かうのが分かりやすそう。案内表示見てみて。"

    service.handle(event("station-from-tokyo", "品川駅がどこか分からない"))

    assert ai.calls[0][2]["recent_messages"][0]["message_text"] == "今東京駅"
    assert ai.research_calls
    assert "今どこ" not in line.pushes[0][1]


def test_station_where_can_answer_without_clarification_or_search():
    decision = proactive_decision(
        HelpType.NAVIGATION,
        "品川駅の所在地を知りたい",
        "品川駅は東京都港区にある駅だよ。",
        confidence=0.92,
        helpfulness=0.85,
        risk=0.1,
        friction=0.6,
    )
    decision.user_goal = "品川駅の所在地を知る"
    decision.known_facts = ["対象は品川駅"]
    decision.can_answer_without_clarification = True
    service, _, ai, line = make_service([decision])

    service.handle(event("station-where", "品川駅ってどこ？"))

    assert ai.research_calls == []
    assert "東京都港区" in line.pushes[0][1]


def test_station_exit_with_sufficient_intent_can_research():
    decision = proactive_decision(
        HelpType.NAVIGATION,
        "品川駅構内で港南口への方向を知りたい",
        None,
        web=True,
        confidence=0.92,
        helpfulness=0.9,
        risk=0.1,
        friction=0.8,
        level=HelpLevel.WEB_RESEARCH,
    )
    decision.user_goal = "品川駅の港南口へ出る"
    decision.known_facts = ["品川駅にいる", "目的地は港南口"]
    decision.can_answer_without_clarification = True
    decision.research_ready = True
    service, _, ai, line = make_service([decision])
    ai.research_result = ConversationResearch(answer_summary="港南口への案内", sources=[])
    ai.rendered = "中央改札を出たら港南口の案内を追えば大丈夫そう。"

    service.handle(event("station-exit", "品川駅の港南口どっち？"))

    assert ai.research_calls
    assert line.pushes


@pytest.mark.parametrize(
    ("text", "goal", "question", "blocking"),
    [
        ("遅刻しそう", "目的地への到着と遅刻連絡を助ける", "どこ向かってる？", ["destination"]),
        ("明日傘いるかな", "明日の天気から傘が必要か判断する", "明日どのへん行く予定？", ["location"]),
        ("これ高すぎない？", "価格の妥当性を確認する", "何の値段？", ["priced_item"]),
        ("道分からない", "目的地までの行き方を知る", "どこ行きたい？", ["destination"]),
        ("子ども飽きてきた", "近くで子どもが楽しめる行動を探す", "今どのへんいる？", ["location"]),
    ],
)
def test_generic_blocking_information_asks_only_one_minimal_question(text, goal, question, blocking):
    decision = clarification_decision(goal, goal, question, blocking)
    service, _, ai, line = make_service([decision], cooldown=0)

    service.handle(event(f"clarify-{abs(hash(text))}", text))

    assert ai.research_calls == []
    assert line.pushes == [("G1", question)]


def test_food_question_then_short_cook_answer_is_follow_up_with_assistant_history():
    first = proactive_decision(
        HelpType.FOOD,
        "今すぐ食事を用意したい",
        "出前にする？ それとも今すぐ買う／作る？",
        confidence=0.82,
        helpfulness=0.85,
        risk=0.15,
    )
    first.topic_summary = "空腹で、すぐ食べられる方法を検討中"
    first.user_goal = "今すぐ食事を用意したい"
    first.pending_question = "出前にする？ それとも今すぐ買う／作る？"
    first.pending_question_type = "MEAL_METHOD"
    first.pending_options = ["DELIVERY", "BUY_NOW", "COOK_NOW"]
    first.expected_response_types = ["選択肢"]

    def second(context):
        topic = context["active_topics"][0]
        follow = follow_up_decision(
            "じゃあ速攻で作れるやつにしよ。冷蔵庫に何ある？",
            known_facts=["自炊を選択", "今すぐ作りたい"],
            pending_question="冷蔵庫に何ある？",
        )
        follow.topic_id = topic["topic_id"]
        return follow

    service, db, ai, line = make_service([first, second])
    service.handle(event("food-start", "お腹空いたなー"))
    service.handle(event("food-cook", "今すぐ作りたいの"))

    assert len(line.pushes) == 2
    assert "冷蔵庫" in line.pushes[1][1]
    second_context = ai.calls[1][2]
    assert second_context["recent_messages"][-1]["role"] == "assistant"
    assert "出前にする" in second_context["recent_messages"][-1]["message_text"]
    assert second_context["active_topics"][0]["pending_options"] == ["DELIVERY", "BUY_NOW", "COOK_NOW"]
    assert db.active_conversation_topics("G1", "U1", 60)[0]["known_facts"] == ["自炊を選択", "今すぐ作りたい"]


def test_food_topic_continues_for_ingredients_then_selected_recipe():
    first = proactive_decision(HelpType.FOOD, "食事を作りたい", "冷蔵庫に何ある？")
    first.pending_question = "冷蔵庫に何ある？"
    first.topic_summary = "すぐ作れる食事を相談中"
    ingredients = follow_up_decision(
        "それなら卵かけご飯かチャーハンが早い。どっちにする？",
        known_facts=["卵とご飯がある"],
        pending_question="卵かけご飯とチャーハン、どっちにする？",
    )
    recipe = follow_up_decision(
        "いいね。卵炒めて、ご飯入れて、塩こしょう＋しょうゆちょいで十分うまい。",
        known_facts=["卵とご飯がある", "チャーハンを選択"],
    )
    service, _, _, line = make_service([first, ingredients, recipe])

    service.handle(event("cook-start", "すぐ作れるの教えて"))
    service.handle(event("cook-eggs", "卵とご飯ならある"))
    service.handle(event("cook-choice", "チャーハンにする"))

    assert len(line.pushes) == 3
    assert "どっちにする" in line.pushes[1][1]
    assert "しょうゆ" in line.pushes[2][1]


def test_active_food_topic_does_not_force_unrelated_weather_into_follow_up():
    first = proactive_decision(HelpType.FOOD, "食事を作りたい", "何作る？")
    first.pending_question = "何作る？"
    weather = proactive_decision(
        HelpType.WEATHER,
        "明日の天気を知りたい",
        "どのへんの天気見ればいい？",
        confidence=0.85,
        helpfulness=0.8,
        risk=0.15,
    )
    service, _, _, line = make_service([first, weather])

    service.handle(event("topic-food", "腹減った"))
    service.handle(event("topic-weather", "そういえば明日雨かな"))

    assert len(line.pushes) == 2
    assert "天気" in line.pushes[1][1]


@pytest.mark.parametrize(
    ("question", "answer", "reply"),
    [
        ("AとBどっち？", "そっち", "じゃあBでいこ。"),
        ("どれくらい時間ある？", "10分くらい", "10分ならすぐできるやつに絞ろう。"),
    ],
)
def test_short_answer_is_understood_when_pending_question_exists(question, answer, reply):
    first = proactive_decision(HelpType.OTHER, "選択を決めたい", question)
    first.pending_question = question
    first.pending_options = ["A", "B"]
    follow = follow_up_decision(reply)
    service, _, _, line = make_service([first, follow])

    service.handle(event(f"short-q-{abs(hash(question))}", "どうしよう"))
    service.handle(event(f"short-a-{abs(hash(answer))}", answer))

    assert line.pushes[-1][1] == reply


def test_topic_is_scoped_to_primary_user_and_can_be_closed():
    first = proactive_decision(HelpType.FOOD, "U1の食事相談", "何作る？")
    first.pending_question = "何作る？"
    close = follow_up_decision("了解、また困ったら呼んで。", close=True)
    service, db, ai, line = make_service([first, ConversationDecision(), close])

    service.handle(event("user-topic", "腹減った", "U1"))
    service.handle(event("other-user", "10分くらい", "U2"))
    service.handle(event("close-topic", "ありがとう、もう大丈夫", "U1"))

    assert ai.calls[1][2]["active_topics"] == []
    assert db.active_conversation_topics("G1", "U1", 60) == []
    assert line.pushes[-1][1] == "了解、また困ったら呼んで。"


def test_active_topic_expires_after_configured_ttl():
    service, db, _, _ = make_service([])
    db.ensure_group("G1", "group")
    topic_id = db.upsert_conversation_topic(
        "G1",
        "U1",
        topic_id=None,
        topic_summary="古い相談",
        user_goal="古い相談を続ける",
        known_facts=[],
        open_questions=[],
        pending_question="続ける？",
        pending_question_type="CHOICE",
        pending_options=["続ける", "やめる"],
        expected_response_types=["選択肢"],
    )
    old = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE conversation_topics SET last_activity_at=? WHERE topic_id=?", (old, topic_id))

    assert db.active_conversation_topics("G1", "U1", service.active_topic_ttl_minutes) == []


def test_natural_hunger_statement_reaches_latent_analysis_and_gets_light_help():
    decision = proactive_decision(
        HelpType.FOOD,
        "今すぐ何か食べたい可能性が高い",
        "お腹空いたね。すぐ作るなら、家にあるもので早いやつ一緒に考える？",
        confidence=0.78,
        helpfulness=0.78,
        risk=0.18,
        discomfort=0.45,
        friction=0.35,
        level=HelpLevel.LIGHT,
    )
    service, _, ai, line = make_service([decision])

    service.handle(event("hunger-ne", "お腹空いたねー"))

    assert ai.calls[0][2]["lightweight_need_signals"]["potential_need_language"] is True
    assert decision.explicit_help_request is False
    assert line.pushes == [("G1", decision.reply_text)]


def test_natural_hunger_statement_recovers_when_model_returns_no_action():
    service, db, ai, line = make_service([ConversationDecision()])

    service.handle(event("hunger-ne-recovery", "お腹空いたねー"))

    assert len(ai.calls) == 1
    assert line.pushes
    assert "お腹空いた" in line.pushes[0][1]
    assert "どれがいい" in line.pushes[0][1]
    topics = db.active_conversation_topics("G1", "U1", 60)
    assert topics[0]["pending_options"] == ["作る", "買う", "近くで探す"]
