import logging

from app.ai.client import AIClient
from app.line.client import LineClient
from app.models import Action
from app.repositories.database import Database

logger = logging.getLogger(__name__)


class KanjiService:
    def __init__(self, db: Database, ai: AIClient, line: LineClient):
        self.db, self.ai, self.line = db, ai, line

    def handle(self, event: dict) -> None:
        message = event.get("message", {})
        if event.get("type") != "message" or message.get("type") != "text":
            return
        source = event.get("source", {})
        source_type = source.get("type", "user")
        conversation_id = source.get("groupId") or source.get("roomId") or source.get("userId")
        user_id = source.get("userId") or "unknown"
        message_id = message.get("id", "")
        event_key = event.get("webhookEventId") or message_id
        if not conversation_id or not event_key or not self.db.claim_message(event_key, message_id):
            return
        self.db.ensure_group(conversation_id, source_type)
        display_name = self.line.display_name(user_id, source_type, conversation_id)
        context = self.db.context(conversation_id)
        decision = self.ai.decide(message.get("text", ""), display_name, context)
        event_row = self.db.active_event(conversation_id)
        if decision.create_event and not event_row:
            event_id = self.db.create_event(conversation_id, decision.event_title or "みんなの予定", (decision.event_status or "planning").value if hasattr(decision.event_status, "value") else "planning")
        elif event_row:
            event_id = int(event_row["id"])
        else:
            event_id = None
        if event_id:
            participant_id = self.db.ensure_participant(event_id, user_id, display_name)
            self.db.save_decision(event_id, participant_id, decision)
        self.db.add_message(conversation_id, event_id, user_id, display_name, message.get("text", ""), message_id, int(event.get("timestamp", 0)))
        reply = decision.reply_text
        if decision.search_required or decision.action == Action.SEARCH:
            reply = self.ai.search(self.db.context(conversation_id), message.get("text", "")) or "ごめん、今うまく検索できなかった。少し置いてもう一度頼んで〜"
        if decision.reply_required and reply and event.get("replyToken"):
            self.line.reply(event["replyToken"], reply)

