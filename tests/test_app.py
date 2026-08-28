from app.main import service
from app.models import Action, Availability, Decision, Fact


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
    monkeypatch.setattr(service.line, "reply", lambda *_: (_ for _ in ()).throw(AssertionError("must stay silent")))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(
        action=Action.REMEMBER_ONLY, create_event=True, event_title="9月飲み", facts=[Fact(candidate_date="2026-09-19", availability=Availability.yes)]
    ))
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
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.REMEMBER_ONLY, create_event=True, facts=[Fact(candidate_date="9/21", availability="maybe")]))
    post_webhook(event_factory(message_id="a", group_id="GA"))
    post_webhook(event_factory(message_id="b", group_id="GB"))
    assert len(fresh_db.context("GA")["availability"]) == 1
    assert len(fresh_db.context("GB")["availability"]) == 1
    assert fresh_db.context("missing")["event"] is None


def test_reply_action(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "reply", lambda token, text: replies.append((token, text)))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.REPLY, reply_text="いいね、やろう🍺", create_event=True))
    post_webhook(event_factory())
    assert replies == [("reply-m1", "いいね、やろう🍺")]


def test_ignore_action(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "reply", lambda *x: replies.append(x))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.IGNORE))
    assert post_webhook(event_factory()).status_code == 200
    assert replies == []


def test_openai_error_falls_back_to_ignore(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "reply", lambda *x: replies.append(x))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.IGNORE))
    assert post_webhook(event_factory()).status_code == 200
    assert not replies


def test_line_error_does_not_fail_webhook(post_webhook, event_factory, monkeypatch):
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.REPLY, reply_text="返信", create_event=True))
    monkeypatch.setattr(service.line, "reply", lambda *_: (_ for _ in ()).throw(RuntimeError("LINE down")))
    assert post_webhook(event_factory()).status_code == 200


def test_search_action(post_webhook, event_factory, monkeypatch):
    replies = []
    monkeypatch.setattr(service.line, "display_name", lambda *_: "A")
    monkeypatch.setattr(service.line, "reply", lambda token, text: replies.append(text))
    monkeypatch.setattr(service.ai, "decide", lambda *_: Decision(action=Action.SEARCH, search_required=True, create_event=True))
    monkeypatch.setattr(service.ai, "search", lambda *_: "① 実在候補 https://example.com")
    post_webhook(event_factory(text="実際に行ける場所を探して"))
    assert "実在候補" in replies[0]

