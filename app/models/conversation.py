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

    @model_validator(mode="after")
    def normalize(self):
        if self.action == ConversationAction.NO_ACTION:
            self.reply_required = False
            self.reply_text = None
            self.web_search_required = False
        if self.action in {ConversationAction.WEB_RESEARCH, ConversationAction.FACT_CHECK}:
            self.web_search_required = True
        if self.help_level == HelpLevel.WEB_RESEARCH:
            self.web_search_required = True
        return self


class ResearchSource(BaseModel):
    title: str
    url: str
    note: str


class ConversationResearch(BaseModel):
    answer_summary: str
    sources: list[ResearchSource] = Field(default_factory=list, max_length=5)
