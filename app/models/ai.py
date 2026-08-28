from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Action(str, Enum):
    IGNORE = "IGNORE"
    REMEMBER_ONLY = "REMEMBER_ONLY"
    REPLY = "REPLY"
    ORGANIZE = "ORGANIZE"
    SEARCH = "SEARCH"
    SEARCH_VENUE = "SEARCH_VENUE"
    PROPOSE = "PROPOSE"


class Availability(str, Enum):
    yes = "yes"
    no = "no"
    maybe = "maybe"
    unknown = "unknown"


class EventStatus(str, Enum):
    planning = "planning"
    scheduling = "scheduling"
    venue_search = "venue_search"
    decided = "decided"
    cancelled = "cancelled"


class Fact(BaseModel):
    candidate_date: str = Field(description="ISO date if certain; otherwise the original Japanese expression")
    availability: Availability
    note: str | None = None


class PreferenceUpdate(BaseModel):
    area: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    number_of_people: int | None = None
    preferred_food: str | None = None
    disliked_food: str | None = None
    atmosphere: str | None = None
    start_time: str | None = None
    other_requirements: str | None = None


class Decision(BaseModel):
    action: Action
    reply_required: bool = False
    reply_text: str | None = None
    facts: list[Fact] = Field(default_factory=list)
    search_required: bool = False
    create_event: bool = False
    event_title: str | None = None
    event_status: EventStatus | None = None
    preference_update: PreferenceUpdate | None = None
    event_summary: str | None = None

    @model_validator(mode="after")
    def normalize(self):
        if self.action in {Action.REPLY, Action.ORGANIZE, Action.PROPOSE, Action.SEARCH, Action.SEARCH_VENUE}:
            self.reply_required = True
        if not self.reply_required:
            self.reply_text = None
        return self
