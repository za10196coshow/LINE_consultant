from app.repositories.database import Database
from app.services.coordinator import ResponseCoordinator


def event(message_id, text):
    return {
        "type": "message",
        "webhookEventId": f"evt-{message_id}",
        "timestamp": 1,
        "source": {"type": "group", "groupId": "G1", "userId": "U1"},
        "message": {"id": message_id, "type": "text", "text": text},
    }


class FakeHandler:
    def __init__(self):
        self.events = []

    def handle_safely(self, value):
        self.events.append(value)


class FakeLine:
    def display_name(self, *_):
        return "A"


def test_coordinator_selects_exactly_one_responder():
    db = Database(":memory:")
    organizer = FakeHandler()
    assistant = FakeHandler()
    coordinator = ResponseCoordinator(db, FakeLine(), organizer, assistant)

    coordinator.handle(event("m1", "9月に飲もう"))
    coordinator.handle(event("m2", "iPhoneの設定どうやる？"))

    assert len(organizer.events) == 1
    assert len(assistant.events) == 1


def test_no_action_records_context_without_calling_either_ai_service():
    db = Database(":memory:")
    organizer = FakeHandler()
    assistant = FakeHandler()
    coordinator = ResponseCoordinator(db, FakeLine(), organizer, assistant)

    coordinator.handle(event("m1", "ありがとう"))

    assert organizer.events == []
    assert assistant.events == []
    assert db.conversation_context("G1")["recent_messages"][0]["message_text"] == "ありがとう"


def test_food_venue_overlap_still_selects_only_organizer():
    db = Database(":memory:")
    organizer = FakeHandler()
    assistant = FakeHandler()
    coordinator = ResponseCoordinator(db, FakeLine(), organizer, assistant)

    coordinator.handle(event("m3", "腹減ったから店探そう"))

    assert len(organizer.events) == 1
    assert assistant.events == []
