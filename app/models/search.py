from dataclasses import dataclass

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


@dataclass(frozen=True)
class VenueSearchResult:
    text: str
    source_urls: tuple[str, ...]
    venues_found: int
