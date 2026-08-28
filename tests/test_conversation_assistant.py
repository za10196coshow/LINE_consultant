from app.ai.budget import ApiBudget, ApiBudgetExceeded
from app.models import ConversationAction, ConversationDecision, ConversationResearch, HelpLevel, HelpType, ResearchSource
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
        self.rendered = None

    def analyze(self, message, display_name, context):
        self.calls.append((message, display_name, context))
        item = self.decisions.pop(0)
        return item(context) if callable(item) else item

    def research(self, message, context):
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
