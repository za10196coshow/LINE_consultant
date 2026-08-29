from enum import Enum

from pydantic import BaseModel, Field, model_validator


class MessageRoute(str, Enum):
    ORGANIZER = "ORGANIZER"
    CONVERSATION_ASSISTANT = "CONVERSATION_ASSISTANT"
    NO_ACTION = "NO_ACTION"


class ConversationAction(str, Enum):
    NO_ACTION = "NO_ACTION"
    ANSWER_QUESTION = "ANSWER_QUESTION"
    CLARIFY_CONFLICT = "CLARIFY_CONFLICT"
    SUMMARIZE_STATE = "SUMMARIZE_STATE"
    RESOLVE_ISSUE = "RESOLVE_ISSUE"
    REQUEST_MISSING_INFO = "REQUEST_MISSING_INFO"
    UNANSWERED_QUESTION = "UNANSWERED_QUESTION"
    WEB_RESEARCH = "WEB_RESEARCH"
    FACT_CHECK = "FACT_CHECK"
    POTENTIAL_NEED = "POTENTIAL_NEED"
    PROACTIVE_HELP = "PROACTIVE_HELP"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"
    FOLLOW_UP = "FOLLOW_UP"


class HelpType(str, Enum):
    NONE = "NONE"
    FOOD = "FOOD"
    WEATHER = "WEATHER"
    DELAY = "DELAY"
    TRANSPORT = "TRANSPORT"
    BATTERY = "BATTERY"
    DEVICE = "DEVICE"
    ACTIVITY = "ACTIVITY"
    NAVIGATION = "NAVIGATION"
    SAFETY = "SAFETY"
    OTHER = "OTHER"


class HelpLevel(int, Enum):
    NONE = 0
    LIGHT = 1
    ADVICE = 2
    WEB_RESEARCH = 3
    ACTIVE_SUPPORT = 4


class IssueStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    OBSOLETE = "OBSOLETE"


class ConversationDecision(BaseModel):
    action: ConversationAction = ConversationAction.NO_ACTION
    reply_required: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    reply_text: str | None = None
    topic: str | None = None
    issue_type: str | None = None
    summary: str | None = None
    web_search_required: bool = False
    resolves_issue_id: int | None = None
    human_answer_in_progress: bool = False
    expected_helpfulness: float = Field(default=0.0, ge=0.0, le=1.0)
    intrusiveness_risk: float = Field(default=1.0, ge=0.0, le=1.0)
    help_type: HelpType = HelpType.NONE
    help_level: HelpLevel = HelpLevel.NONE
    latent_need: str | None = None
    need_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    urgency: float = Field(default=0.0, ge=0.0, le=1.0)
    actionability: float = Field(default=0.0, ge=0.0, le=1.0)
    information_needed: list[str] = Field(default_factory=list, max_length=8)
    external_research_needed: bool = False
    suggested_action: str | None = None
    need_category: str | None = None
    explicit_help_request: bool = False
    discomfort_signal: float = Field(default=0.0, ge=0.0, le=1.0)
    friction_signal: float = Field(default=0.0, ge=0.0, le=1.0)
    user_goal: str | None = None
    known_facts: list[str] = Field(default_factory=list, max_length=12)
    missing_information: list[str] = Field(default_factory=list, max_length=12)
    blocking_missing_information: list[str] = Field(default_factory=list, max_length=6)
    can_answer_without_clarification: bool = True
    clarification_question: str | None = None
    top_intent_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    research_ready: bool = False
    topic_id: str | None = None
    topic_summary: str | None = None
    continuation_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    resolved_reference: str | None = None
    pending_question: str | None = None
    pending_question_type: str | None = None
    pending_options: list[str] = Field(default_factory=list, max_length=8)
    expected_response_types: list[str] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=8)
    close_topic: bool = False

    @model_validator(mode="after")
    def normalize(self):
        clarification_needed = (
            bool(self.latent_need or self.user_goal)
            and bool(self.blocking_missing_information)
            and not self.can_answer_without_clarification
            and bool(self.clarification_question or self.reply_text)
        )
        if self.action == ConversationAction.ASK_CLARIFICATION or clarification_needed:
            self.action = ConversationAction.ASK_CLARIFICATION
            self.reply_required = True
            self.reply_text = self.reply_text or self.clarification_question
            self.external_research_needed = False
            self.web_search_required = False
            self.research_ready = False
        if self.action == ConversationAction.FOLLOW_UP:
            self.reply_required = not self.close_topic or bool(self.reply_text)
        implicit_need_is_actionable = (
            bool(self.latent_need)
            and (self.discomfort_signal >= 0.6 or self.friction_signal >= 0.65)
            and self.expected_helpfulness >= 0.6
            and self.intrusiveness_risk <= 0.5
            and bool(self.reply_text or self.external_research_needed)
        )
        if self.action == ConversationAction.NO_ACTION and implicit_need_is_actionable:
            self.action = ConversationAction.PROACTIVE_HELP
            self.reply_required = True
        if self.action == ConversationAction.NO_ACTION:
            self.reply_required = False
            self.reply_text = None
            self.web_search_required = False
        if self.action in {ConversationAction.WEB_RESEARCH, ConversationAction.FACT_CHECK}:
            self.web_search_required = True
        if self.help_level == HelpLevel.WEB_RESEARCH:
            self.web_search_required = True
        if self.external_research_needed:
            self.web_search_required = True
        if self.action == ConversationAction.ASK_CLARIFICATION:
            self.web_search_required = False
            self.external_research_needed = False
            self.research_ready = False
        elif self.web_search_required:
            self.research_ready = not self.blocking_missing_information and self.can_answer_without_clarification
        if self.action in {
            ConversationAction.POTENTIAL_NEED,
            ConversationAction.PROACTIVE_HELP,
            ConversationAction.ASK_CLARIFICATION,
        }:
            if self.need_confidence == 0:
                self.need_confidence = self.confidence
            if self.actionability == 0:
                self.actionability = self.expected_helpfulness
        return self


class ResearchSource(BaseModel):
    title: str
    url: str
    note: str


class ConversationResearch(BaseModel):
    answer_summary: str
    sources: list[ResearchSource] = Field(default_factory=list, max_length=5)
