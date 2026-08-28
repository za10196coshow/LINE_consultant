from pydantic import BaseModel, Field


class VenueSearchCriteria(BaseModel):
    location: str | None = None
    party_size: int | None = Field(default=None, ge=1)
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    genre: str | None = None
    candidate_dates: list[str] = Field(default_factory=list)
    atmosphere: str | None = None
    start_time: str | None = None
    requirements: str | None = None


class VenueCandidate(BaseModel):
    name: str
    area: str | None = None
    genre: str | None = None
    budget: str | None = None
    reason: str
    url: str
    source: str | None = None


class VenueCandidatePayload(BaseModel):
    candidates: list[VenueCandidate] = Field(default_factory=list, max_length=5)


class VenueSearchResult(BaseModel):
    candidates: list[VenueCandidate] = Field(default_factory=list, max_length=3)
    source_urls: list[str] = Field(default_factory=list)

    @property
    def venues_found(self) -> int:
        return len(self.candidates)
