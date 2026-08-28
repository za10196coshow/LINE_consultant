from app.ai.budget import ApiBudgetExceeded
from app.ai.client import OpenAITimeoutExhausted
from app.main import service
from app.models import Action, Availability, Decision, Fact, PreferenceUpdate, VenueCandidate, VenueSearchResult


def test_root(client):
    assert client.get("/").json() == {"service": "LINE AI Kanji", "status": "running"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_bad_signature(client):
    response = client.post("/webhook", content=b'{"events":[]}', headers={"x-line-signature": "bad"})
    assert response.status_code == 400


def test_valid_signature_empty_events(post_webhook):
    assert post_webhook({"events": []}).status_code == 200


def test_webhook_remember_only_and_duplicate(post_webhook, event_factory, monkeypatch, fresh_db):
    monkeypatch.setattr(service.line, "display_name", lambda *_: "田中")
    monkeypatch.setattr(service.line, "push", lambda *_: (_ for _ in ()).throw(AssertionError("must stay silent")))
    monkeypatch.setattr(
        service.ai,
        "decide",
        lambda *_: Decision(
            action=Action.REMEMBER_ONLY,
            create_event=True,
            event_title="9月飲み",
            facts=[Fact(candidate_date="2026-09-19", availability=Availability.yes)],
        ),
    )
    body = event_factory(text="19日なら行ける")
    assert post_webhook(body).status_code == 200
    assert post_webhook(body).status_code == 200
    ctx = fresh_db.context("G1")
    assert len(ctx["availability"]) == 1
    assert ctx["availability"][0]["display_name"] == "田中"
    assert ctx["availability"][0]["availability"] == "yes"
    with fresh_db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM processed_messages").fetchone()[0] == 1


def test_group_data_isolation(post_webhook, event_factory, monkeypatch, fresh_db):
    monkeypatch.setattr(service.line, "display_name", lambda *_: "参加者")
    monkeypatch.setattr(
        service.ai,
        "decide",
        lambda *_: Decision(action=Action.REMEMBER_ONLY, create_event=True, facts=[Fact(candidate_date="9/21", availability="maybe")]),
    )
    post_webhook(event_factory(message_id="a", group_id="GA"))
    post_webhook(event_factory(message_id="b", group_id="GB"))
    assert len(fresh_db.context("GA")["availability"]) == 1
    assert len(fresh_db.context("GB")["availability"]) == 1
    assert fresh_db.context("missing")["event"] is None


def test_reply_action_uses_push_after_background_processing(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "push", lambda target, text: replies.append((target, text)))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.REPLY, reply_text="いいね、やろう🍺", create_event=True))
    post_webhook(event_factory())
    assert replies == [("G1", "いいね、やろう🍺")]


def test_ignore_action(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "push", lambda *x: replies.append(x))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.IGNORE))
    assert post_webhook(event_factory()).status_code == 200
    assert replies == []


def test_openai_error_falls_back_to_ignore(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "push", lambda *x: replies.append(x))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.IGNORE))
    assert post_webhook(event_factory()).status_code == 200
    assert not replies


def test_line_error_does_not_fail_webhook(post_webhook, event_factory, monkeypatch):
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.REPLY, reply_text="返信", create_event=True))
    monkeypatch.setattr(service.line, "push", lambda *_: False)
    assert post_webhook(event_factory()).status_code == 200


def test_search_action(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "push", lambda target, text: replies.append(text))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.SEARCH, search_required=True, create_event=True))
    monkeypatch.setattr(
        service.ai,
        "search",
        lambda *_: VenueSearchResult(
            candidates=[
                VenueCandidate(name="店A", reason="駅近", url="https://example.com/a"),
                VenueCandidate(name="店B", reason="落ち着く", url="https://example.com/b"),
                VenueCandidate(name="店C", reason="ワイワイ", url="https://example.com/c"),
            ],
            source_urls=["https://example.com/a", "https://example.com/b", "https://example.com/c"],
        ),
    )
    monkeypatch.setattr(
        service.ai,
        "render_venue_reply",
        lambda *_: "このへん良さそう！\n① 店A 説明 https://example.com/a\n② 店B 説明 https://example.com/b\n③ 店C 説明 https://example.com/c",
    )
    post_webhook(event_factory(text="実際に行ける場所を探して"))
    assert all(marker in replies[0] for marker in ("①", "②", "③"))


def test_timeout_exhausted_returns_200_and_pushes_fallback(post_webhook, event_factory, monkeypatch):
    pushes = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.ai, "decide", lambda *_: (_ for _ in ()).throw(OpenAITimeoutExhausted()))
    monkeypatch.setattr(service.line, "push", lambda target, text: pushes.append((target, text)))

    response = post_webhook(event_factory(text="今どんな感じ？"))

    assert response.status_code == 200
    assert pushes == [("G1", "ちょっと考えるのに失敗した。もう一回呼んで🙏")]


def test_timeout_on_availability_stays_silent(post_webhook, event_factory, monkeypatch):
    pushes = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.ai, "decide", lambda *_: (_ for _ in ()).throw(OpenAITimeoutExhausted()))
    monkeypatch.setattr(service.line, "push", lambda *args: pushes.append(args))

    assert post_webhook(event_factory(text="19日なら行ける")).status_code == 200
    assert pushes == []


def test_background_failure_is_contained_and_webhook_returns_200(post_webhook, event_factory, monkeypatch):
    monkeypatch.setattr(service, "handle", lambda *_: (_ for _ in ()).throw(RuntimeError("unexpected")))
    assert post_webhook(event_factory()).status_code == 200


def test_shop_search_uses_saved_event_preferences(post_webhook, event_factory, monkeypatch, fresh_db):
    fresh_db.ensure_group("G1", "group")
    event_id = fresh_db.create_event("G1", "9月飲み")
    participant_id = fresh_db.ensure_participant(event_id, "U0", "幹事")
    fresh_db.save_decision(
        event_id,
        participant_id,
        Decision(
            action=Action.REMEMBER_ONLY,
            preference_update=PreferenceUpdate(area="横浜", budget_max=5000, number_of_people=5, preferred_food="焼肉"),
        ),
    )
    captured = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.REPLY, reply_text="探すね"))
    monkeypatch.setattr(
        service.ai,
        "search",
        lambda criteria, request: captured.append((criteria, request))
        or VenueSearchResult(
            candidates=[VenueCandidate(name="店A", reason="駅近", url="https://venue.example/a")],
            source_urls=["https://venue.example/a"],
        ),
    )
    monkeypatch.setattr(service.ai, "render_venue_reply", lambda *_: "店Aは駅近。\nhttps://venue.example/a")
    monkeypatch.setattr(service.line, "push", lambda *_: True)

    assert post_webhook(event_factory(text="店探して")).status_code == 200
    assert captured[0][0].location == "横浜"
    assert captured[0][0].budget_max == 5000
    assert captured[0][0].party_size == 5
    assert captured[0][0].genre == "焼肉"


def test_area_preference_does_not_call_web_search(post_webhook, event_factory, monkeypatch):
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(
        service.ai,
        "decide",
        lambda *_: Decision(
            action=Action.REMEMBER_ONLY,
            create_event=True,
            preference_update=PreferenceUpdate(area="横浜"),
        ),
    )
    monkeypatch.setattr(service.ai, "search", lambda *_: (_ for _ in ()).throw(AssertionError("must not search")))

    assert post_webhook(event_factory(text="横浜がいい")).status_code == 200


def test_web_search_failure_pushes_natural_message_without_raw_error(post_webhook, event_factory, monkeypatch):
    pushes = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.SEARCH_VENUE, search_required=True))
    monkeypatch.setattr(service.ai, "search", lambda *_: None)
    monkeypatch.setattr(service.line, "push", lambda target, text: pushes.append(text))

    assert post_webhook(event_factory(text="店探して")).status_code == 200
    assert "もう一回" in pushes[0]
    assert "APITimeoutError" not in pushes[0]
    assert "{" not in pushes[0]
    assert "http" not in pushes[0]


def test_budget_limit_pushes_fixed_message_once_without_other_ai_calls(post_webhook, event_factory, monkeypatch):
    pushes = []
    calls = 0
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "push", lambda target, text: pushes.append((target, text)) or True)

    def blocked(*_):
        nonlocal calls
        calls += 1
        raise ApiBudgetExceeded

    monkeypatch.setattr(service.ai, "decide", blocked)
    assert post_webhook(event_factory(message_id="budget-1")).status_code == 200
    assert post_webhook(event_factory(message_id="budget-2")).status_code == 200

    assert calls == 2
    assert pushes == [("G1", "今日はAI幹事ちょっと働きすぎたので、続きは明日で🙏")]
