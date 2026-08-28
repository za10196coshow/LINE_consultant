import logging
import time

from app.ai.client import AIClient, OpenAITimeoutExhausted
from app.line.client import LineClient
from app.models import Action
from app.repositories.database import Database
from app.services.venue_search import apply_search_override, build_search_criteria

logger = logging.getLogger(__name__)


class KanjiService:
    def __init__(self, db: Database, ai: AIClient, line: LineClient):
        self.db, self.ai, self.line = db, ai, line

    def handle(self, event: dict) -> None:
        started = time.monotonic()
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
        message_text = message.get("text", "")
        try:
            decision = self.ai.decide(message_text, display_name, context)
        except OpenAITimeoutExhausted:
            notify_timeout = self._should_notify_timeout(message_text)
            self.db.add_message(
                conversation_id,
                context["event"]["id"] if context.get("event") else None,
                user_id,
                display_name,
                message_text,
                message_id,
                int(event.get("timestamp", 0)),
            )
            if notify_timeout:
                self.line.push(conversation_id, "ちょっと考えるのに失敗した。もう一回呼んで🙏")
            logger.warning(
                "Event processing stopped after OpenAI timeout notify=%s elapsed_ms=%d",
                notify_timeout,
                round((time.monotonic() - started) * 1000),
            )
            return
        decision = apply_search_override(message_text, decision)
        logger.info("AI action=%s", decision.action.value)
        event_row = self.db.active_event(conversation_id)
        if decision.create_event and not event_row:
            event_id = self.db.create_event(
                conversation_id,
                decision.event_title or "みんなの予定",
                (decision.event_status or "planning").value if hasattr(decision.event_status, "value") else "planning",
            )
        elif event_row:
            event_id = int(event_row["id"])
        else:
            event_id = None
        if event_id:
            participant_id = self.db.ensure_participant(event_id, user_id, display_name)
            self.db.save_decision(event_id, participant_id, decision)
        self.db.add_message(conversation_id, event_id, user_id, display_name, message_text, message_id, int(event.get("timestamp", 0)))
        reply = decision.reply_text
        if decision.search_required or decision.action in {Action.SEARCH, Action.SEARCH_VENUE}:
            criteria = build_search_criteria(self.db.context(conversation_id))
            logger.info(
                "SEARCH_VENUE triggered location=%s party_size=%s budget=%s genre=%s",
                criteria.location or "unknown",
                criteria.party_size or "unknown",
                criteria.budget_max or criteria.budget_min or "unknown",
                criteria.genre or "unknown",
            )
            search_result = self.ai.search(criteria, message_text)
            if search_result:
                logger.info("SEARCH_VENUE candidates=%d", search_result.venues_found)
                logger.info("SEARCH_VENUE rendering reply")
                reply = self.ai.render_venue_reply(criteria, search_result.candidates, message_text)
                logger.info("SEARCH_VENUE reply generated")
            else:
                logger.warning("web_search failed after retry")
                reply = "ごめん、今ちょっと店検索だけうまくいかなかった🙏\nもう一回やってみる？"
        if decision.reply_required and reply:
            self.line.push(conversation_id, reply)
        logger.info("Event processing complete elapsed_ms=%d", round((time.monotonic() - started) * 1000))

    def handle_safely(self, event: dict) -> None:
        try:
            self.handle(event)
        except Exception as exc:
            logger.error("Background event processing failed category=%s", type(exc).__name__)

    @staticmethod
    def _should_notify_timeout(message: str) -> bool:
        cues = ("?", "？", "どう", "どんな", "まとめ", "教えて", "探して", "提案", "候補", "企画", "お願い", "決め", "今ど")
        return any(cue in message for cue in cues)
