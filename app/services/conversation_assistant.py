import hashlib
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from app.ai.budget import ApiBudgetExceeded
from app.ai.conversation import ConversationAIClient
from app.line.client import LineClient
from app.models import ConversationAction, HelpLevel, HelpType, IssueStatus
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
        proactive_threshold: float = 0.65,
        need_confidence_threshold: float = 0.55,
        expected_helpfulness_threshold: float = 0.60,
        intrusiveness_risk_max: float = 0.50,
        intervention_score_threshold: float = 0.55,
    ):
        self.db, self.ai, self.line, self.bot_name = db, ai, line, bot_name
        self.cooldown_minutes = cooldown_minutes
        self.unanswered_delay_seconds = unanswered_delay_seconds
        self.unanswered_delay_messages = unanswered_delay_messages
        self.confidence_threshold = confidence_threshold
        self.proactive_threshold = proactive_threshold
        self.need_confidence_threshold = need_confidence_threshold
        self.expected_helpfulness_threshold = expected_helpfulness_threshold
        self.intrusiveness_risk_max = intrusiveness_risk_max
        self.intervention_score_threshold = intervention_score_threshold

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
        context["event_context"] = self.db.context(conversation_id)
        decision = self.ai.analyze(text, display_name, context)
        decision = self._normalize_latent_decision(decision)
        if decision.user_goal:
            logger.info("USER_GOAL_INFERRED goal=%s", _log_value(decision.user_goal))
        if decision.missing_information or decision.blocking_missing_information:
            logger.info(
                "MISSING_INFORMATION_ANALYSIS missing_count=%d blocking_count=%d blocking_fields=%s",
                len(decision.missing_information),
                len(decision.blocking_missing_information),
                ",".join(_log_value(value) for value in decision.blocking_missing_information) or "none",
            )
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
        proactive = decision.action in {
            ConversationAction.POTENTIAL_NEED,
            ConversationAction.PROACTIVE_HELP,
            ConversationAction.ASK_CLARIFICATION,
        }
        if proactive:
            score = self._intervention_score(decision)
            logger.info(
                "LATENT_NEED_ANALYSIS need_category=%s explicit_help_request=%s need_confidence=%.2f "
                "discomfort_signal=%.2f friction_signal=%.2f expected_helpfulness=%.2f "
                "intrusiveness_risk=%.2f urgency=%.2f actionability=%.2f score=%.3f",
                decision.need_category or decision.help_type.value,
                str(decision.explicit_help_request).lower(),
                decision.need_confidence,
                decision.discomfort_signal,
                decision.friction_signal,
                decision.expected_helpfulness,
                decision.intrusiveness_risk,
                decision.urgency,
                decision.actionability,
                score,
            )
            reason = self._proactive_skip_reason(decision, explicit or decision.explicit_help_request, score)
            if reason:
                logger.info("INTERVENTION_DECISION intervene=false reason=%s score=%.3f", reason, score)
                logger.info("PROACTIVE_HELP_SKIPPED reason=%s", reason)
                return
        issue = self._save_issue(conversation_id, message_id, decision)
        if issue["status"] != IssueStatus.OPEN.value or issue.get("last_notified_at"):
            logger.info("PROACTIVE_HELP_SKIPPED reason=duplicate" if proactive else "CONVERSATION_ASSISTANT_SKIPPED reason=duplicate")
            return
        threshold = 0.55 if explicit else self.confidence_threshold
        if not proactive and (decision.confidence < threshold or not decision.reply_required):
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=low_confidence_or_reply_not_required")
            return
        if decision.human_answer_in_progress:
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=human_answer_in_progress")
            return
        if not explicit and not proactive and self._waiting_for_human(issue):
            logger.info("CONVERSATION_ASSISTANT_SKIPPED reason=waiting_for_human_answer")
            return
        bypass_cooldown = self._can_bypass_cooldown(conversation_id, decision)
        if not explicit and not bypass_cooldown and self._cooldown_active(conversation_id):
            logger.info("PROACTIVE_HELP_SKIPPED reason=cooldown" if proactive else "CONVERSATION_ASSISTANT_SKIPPED reason=cooldown")
            return

        reply = decision.reply_text
        if decision.action == ConversationAction.ASK_CLARIFICATION:
            logger.info("CLARIFICATION_DECISION ask=true")
            logger.info("RESEARCH_READY=false reason=blocking_missing_information")
        if proactive:
            logger.info(
                "INTERVENTION_DECISION intervene=true reason=useful_latent_need score=%.3f help_level=%d needs_web_search=%s",
                self._intervention_score(decision),
                decision.help_level.value,
                decision.web_search_required,
            )
        if decision.web_search_required and not decision.research_ready:
            logger.info("RESEARCH_READY=false reason=missing_information")
            reply = decision.reply_text or decision.clarification_question
        elif decision.web_search_required:
            logger.info("RESEARCH_READY=true")
            logger.info("WEB_SEARCH_STARTED help_type=%s", decision.help_type.value)
            research_context = self.db.conversation_context(conversation_id)
            research_context["event_context"] = self.db.context(conversation_id)
            research_context["latent_need"] = decision.latent_need
            research_context["user_goal"] = decision.user_goal
            research_context["known_facts"] = decision.known_facts
            research_context["information_needed"] = decision.information_needed
            research_context["missing_information"] = decision.missing_information
            research_context["suggested_action"] = decision.suggested_action
            research = self.ai.research(text, research_context)
            if research is None:
                logger.warning("RESEARCH_RESULT status=SEARCH_ERROR_OR_TIMEOUT")
                reply = "ごめん、今ちょっと調べものだけうまくいかなかった🙏 もう一回聞いてみて。"
            else:
                status = "SUCCESS" if research.sources else "NO_RESULTS"
                logger.info("RESEARCH_RESULT status=%s sources=%d", status, len(research.sources))
                reply = self.ai.render_research_reply(text, research)
                logger.info("CHARACTER_REPLY_GENERATED")
        if reply:
            if self.line.push(conversation_id, reply):
                self.db.mark_conversation_issue_notified(int(issue["id"]))
                logger.info("PROACTIVE_HELP_REPLY_SENT" if proactive else "CONVERSATION_ASSISTANT_REPLY_SENT")
                logger.info("LINE_REPLY_SENT")
                if decision.action == ConversationAction.ASK_CLARIFICATION:
                    logger.info("CLARIFICATION_REPLY_SENT")
            else:
                logger.warning("CONVERSATION_ASSISTANT_SKIPPED reason=line_push_failed")

    def _save_issue(self, group_id: str, message_id: str, decision) -> dict:
        topic = decision.topic or decision.latent_need or decision.need_category or "general"
        topic = topic[:120]
        issue_type = decision.issue_type or decision.action.value
        summary = decision.summary or decision.latent_need or decision.reason or "会話上の未解決事項"
        normalized = re.sub(r"\s+", "", f"{self.ai.budget.date_jst}|{topic}|{issue_type}|{summary}").lower()
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
        for existing in self.db.open_conversation_issues(group_id):
            similarity = SequenceMatcher(None, _normalize(existing["summary"]), _normalize(summary)).ratio()
            same_day = _jst_date(existing["created_at"], self.ai.budget.timezone) == self.ai.budget.date_jst
            if same_day and existing["topic"] == topic and existing["issue_type"] == issue_type and similarity >= 0.6:
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

    def _normalize_latent_decision(self, decision):
        if decision.latent_need and decision.action in {
            ConversationAction.WEB_RESEARCH,
            ConversationAction.FACT_CHECK,
            ConversationAction.POTENTIAL_NEED,
        }:
            decision.action = ConversationAction.PROACTIVE_HELP
        if decision.action in {ConversationAction.PROACTIVE_HELP, ConversationAction.ASK_CLARIFICATION}:
            decision.need_confidence = decision.need_confidence or decision.confidence
            decision.actionability = decision.actionability or decision.expected_helpfulness
            decision.web_search_required = decision.web_search_required or decision.external_research_needed
        return decision

    def _intervention_score(self, decision) -> float:
        score = (
            0.30 * decision.need_confidence
            + 0.25 * decision.expected_helpfulness
            + 0.20 * decision.actionability
            + 0.15 * decision.discomfort_signal
            + 0.05 * decision.friction_signal
            + 0.10 * decision.urgency
            - 0.15 * decision.intrusiveness_risk
            + (0.08 if decision.explicit_help_request else 0.0)
        )
        return min(max(score, 0.0), 1.0)

    def _proactive_skip_reason(self, decision, explicit: bool, score: float) -> str | None:
        if not decision.reply_required:
            return "reply_not_required"
        need_threshold = 0.50 if explicit else self.need_confidence_threshold
        if decision.need_confidence < need_threshold:
            return "low_need_confidence"
        if decision.expected_helpfulness < self.expected_helpfulness_threshold:
            return "low_expected_helpfulness"
        if decision.intrusiveness_risk > self.intrusiveness_risk_max:
            return "high_intrusiveness_risk"
        if score < self.intervention_score_threshold:
            return "low_intervention_score"
        return None

    def _can_bypass_cooldown(self, group_id: str, decision) -> bool:
        last_topic = self.db.last_conversation_assistant_topic(group_id)
        current_topic = decision.topic or decision.latent_need or decision.need_category
        return (
            decision.help_type == HelpType.SAFETY
            or decision.help_level == HelpLevel.ACTIVE_SUPPORT
            or (decision.expected_helpfulness >= 0.9 and decision.intrusiveness_risk <= 0.25)
            or (bool(last_topic) and bool(current_topic) and current_topic != last_topic)
        )

    def handle_safely(self, event: dict) -> None:
        try:
            self.handle(event)
        except ApiBudgetExceeded:
            logger.info("PROACTIVE_HELP_SKIPPED reason=budget_limit")
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


def _jst_date(value: str, timezone_info) -> str:
    return datetime.fromisoformat(value).astimezone(timezone_info).date().isoformat()


def _log_value(value: str) -> str:
    return re.sub(r"[\r\n\t]+", " ", value).strip()[:120]
