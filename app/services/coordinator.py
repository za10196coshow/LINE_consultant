import logging

from app.line.client import LineClient
from app.models import MessageRoute
from app.repositories.database import Database
from app.services.conversation_assistant import ConversationAssistant
from app.services.kanji import KanjiService
from app.services.routing import MessageRouter, is_weather_candidate

logger = logging.getLogger(__name__)


class ResponseCoordinator:
    def __init__(
        self,
        db: Database,
        line: LineClient,
        organizer: KanjiService,
        conversation_assistant: ConversationAssistant,
        router: MessageRouter | None = None,
    ):
        self.db, self.line = db, line
        self.organizer, self.conversation_assistant = organizer, conversation_assistant
        self.router = router or MessageRouter()

    def handle(self, event: dict) -> None:
        message = event.get("message", {})
        if event.get("type") != "message" or message.get("type") != "text":
            return
        conversation_id = _conversation_id(event)
        if not conversation_id:
            return
        route = self.router.route(
            message.get("text", ""),
            has_open_issues=self.db.has_open_conversation_issues(conversation_id),
            has_active_event=self.db.active_event(conversation_id) is not None,
        )
        logger.info("MESSAGE_ROUTING route=%s", route.value)
        logger.info(
            "CONVERSATION_INPUT_CLASSIFIED route=%s weather_candidate=%s",
            route.value,
            str(is_weather_candidate(message.get("text", ""))).lower(),
        )
        if route == MessageRoute.ORGANIZER:
            self.organizer.handle_safely(event)
        elif route == MessageRoute.CONVERSATION_ASSISTANT:
            self.conversation_assistant.handle_safely(event)
        else:
            self._record_without_ai(event, conversation_id)

    def _record_without_ai(self, event: dict, conversation_id: str) -> None:
        message = event["message"]
        message_id = message.get("id", "")
        event_key = event.get("webhookEventId") or message_id
        if not event_key or not self.db.claim_message(event_key, message_id):
            return
        source = event.get("source", {})
        self.db.ensure_group(conversation_id, source.get("type", "user"))
        user_id = source.get("userId") or "unknown"
        display_name = self.line.display_name(user_id, source.get("type", "user"), conversation_id)
        event_row = self.db.active_event(conversation_id)
        self.db.add_message(
            conversation_id,
            int(event_row["id"]) if event_row else None,
            user_id,
            display_name,
            message.get("text", ""),
            message_id,
            int(event.get("timestamp", 0)),
        )
        logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=lightweight_filter")
        logger.info("PROACTIVE_HELP_SKIPPED reason=pre_filter")

    def handle_safely(self, event: dict) -> None:
        try:
            self.handle(event)
        except Exception as exc:
            logger.error("Response coordinator failed category=%s", type(exc).__name__)


def _conversation_id(event: dict) -> str | None:
    source = event.get("source", {})
    return source.get("groupId") or source.get("roomId") or source.get("userId")
