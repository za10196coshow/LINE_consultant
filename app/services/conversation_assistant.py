import hashlib
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.ai.budget import ApiBudgetExceeded
from app.ai.conversation import ConversationAIClient
from app.line.client import LineClient
from app.models import ConversationAction, IssueStatus
from app.repositories.database import Database
from app.services.routing import is_explicit_assistant_call

logger = logging.getLogger(__name__)


class ConversationAssistant:
    def __init__(
        self,
        db: Database,
        ai: ConversationAIClient,
        line: LineClient,
        bot_name: str,
        cooldown_minutes: int = 20,
        unanswered_delay_seconds: int = 30,
        unanswered_delay_messages: int = 1,
        confidence_threshold: float = 0.78,
    ):
        self.db, self.ai, self.line, self.bot_name = db, ai, line, bot_name
        self.cooldown_minutes = cooldown_minutes
        self.unanswered_delay_seconds = unanswered_delay_seconds
        self.unanswered_delay_messages = unanswered_delay_messages
        self.confidence_threshold = confidence_threshold

    def handle(self, event: dict) -> None:
        message = event.get("message", {})
        source = event.get("source", {})
        conversation_id = source.get("groupId") or source.get("roomId") or source.get("userId")
        message_id = message.get("id", "")
        event_key = event.get("webhookEventId") or message_id
        if not conversation_id or not event_key or not self.db.claim_message(event_key, message_id):
            return
        self.db.ensure_group(conversation_id, source.get("type", "user"))
        user_id = source.get("userId") or "unknown"
        display_name = self.line.display_name(user_id, source.get("type", "user"), conversation_id)
        text = message.get("text", "")
        explicit = is_explicit_assistant_call(text, self.bot_name)
        context = self.db.conversation_context(conversation_id)
        decision = self.ai.analyze(text, display_name, context)
        self.db.add_message(
            conversation_id,
            None,
            user_id,
            display_name,
            text,
            message_id,
            int(event.get("timestamp", 0)),
        )

        if decision.resolves_issue_id:
            self.db.resolve_conversation_issue(decision.resolves_issue_id)
        if decision.action == ConversationAction.NO_ACTION:
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=no_action")
            return

        logger.info(
            "CONVERSATION_ISSUE_DETECTED type=%s confidence=%.2f",
            decision.issue_type or decision.action.value,
            decision.confidence,
        )
        issue = self._save_issue(conversation_id, message_id, decision)
        if issue["status"] != IssueStatus.OPEN.value or issue.get("last_notified_at"):
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=duplicate_or_resolved")
            return
        threshold = 0.55 if explicit else self.confidence_threshold
        if decision.confidence < threshold or not decision.reply_required:
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=low_confidence_or_reply_not_required")
            return
        if decision.human_answer_in_progress:
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=human_answer_in_progress")
            return
        if not explicit and self._waiting_for_human(issue):
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=waiting_for_human_answer")
            return
        if not explicit and self._cooldown_active(conversation_id):
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=cooldown")
            return

        reply = decision.reply_text
        if decision.web_search_required:
            research = self.ai.research(text, self.db.conversation_context(conversation_id))
            if research is None:
                reply = "ごめん、今ちょっと調べものだけうまくいかなかった🙏 もう一回聞いてみて。"
            else:
                reply = self.ai.render_research_reply(text, research)
        if reply:
            if self.line.push(conversation_id, reply):
                self.db.mark_conversation_issue_notified(int(issue["id"]))
                logger.info("CONVERSATION_ASSISTANT_REPLY_SENT")
            else:
                logger.warning("CONVERSATION_ASSISTANT_SKIPPED reason=line_push_failed")

    def _save_issue(self, group_id: str, message_id: str, decision) -> dict:
        topic = decision.topic or "general"
        issue_type = decision.issue_type or decision.action.value
        summary = decision.summary or decision.reason or "会話上の未解決事項"
        normalized = re.sub(r"\s+", "", f"{topic}|{issue_type}|{summary}").lower()
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
        for existing in self.db.open_conversation_issues(group_id):
            similarity = SequenceMatcher(None, _normalize(existing["summary"]), _normalize(summary)).ratio()
            if existing["topic"] == topic and existing["issue_type"] == issue_type and similarity >= 0.6:
                fingerprint = existing["fingerprint"]
                break
        return self.db.upsert_conversation_issue(group_id, fingerprint, topic, issue_type, summary, decision.confidence, message_id)

    def _waiting_for_human(self, issue: dict) -> bool:
        messages = self.db.conversation_messages_since_issue(int(issue["id"]))
        created = datetime.fromisoformat(issue["created_at"])
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        return messages < self.unanswered_delay_messages and elapsed < self.unanswered_delay_seconds

    def _cooldown_active(self, group_id: str) -> bool:
        last = self.db.last_conversation_assistant_notification(group_id)
        if not last:
            return False
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        return elapsed.total_seconds() < self.cooldown_minutes * 60

    def handle_safely(self, event: dict) -> None:
        try:
            self.handle(event)
        except ApiBudgetExceeded:
            conversation_id = _conversation_id(event)
            if conversation_id:
                date_jst = self.ai.budget.date_jst
                if self.db.claim_budget_notification(date_jst, conversation_id):
                    self.line.push(conversation_id, "今日はAI幹事ちょっと働きすぎたので、続きは明日で🙏")
        except Exception as exc:
            logger.error("Conversation assistant processing failed category=%s", type(exc).__name__)


def _conversation_id(event: dict) -> str | None:
    source = event.get("source", {})
    return source.get("groupId") or source.get("roomId") or source.get("userId")


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()
