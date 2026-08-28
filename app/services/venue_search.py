import re

from app.models import Action, Decision, VenueSearchCriteria

_SEARCH_VERBS = ("探して", "探す", "調べて", "調べる", "候補出して", "候補を出して")
_VENUE_WORDS = ("店", "お店", "居酒屋", "焼肉", "レストラン", "二次会", "遊ぶ", "遊び", "BBQ", "バーベキュー", "ところ", "場所")


def is_venue_search_request(message: str) -> bool:
    normalized = re.sub(r"\s+", "", message).lower()
    has_verb = any(verb.lower() in normalized for verb in _SEARCH_VERBS)
    has_target = any(word.lower() in normalized for word in _VENUE_WORDS)
    budget_search = bool(re.search(r"\d[\d,]*円.*探して", normalized))
    condition_candidates = "この条件" in normalized and "候補" in normalized
    good_shop_question = "店ない" in normalized or "お店ない" in normalized
    return (has_verb and has_target) or budget_search or condition_candidates or good_shop_question


def apply_search_override(message: str, decision: Decision) -> Decision:
    if is_venue_search_request(message):
        decision.action = Action.SEARCH_VENUE
        decision.search_required = True
        decision.reply_required = True
        decision.reply_text = None
    return decision


def build_search_criteria(context: dict) -> VenueSearchCriteria:
    preferences = context.get("preferences") or {}
    availability = context.get("availability") or []
    participant_names = {row.get("display_name") for row in availability if row.get("display_name")}
    dates = sorted(
        {row["candidate_date"] for row in availability if row.get("candidate_date") and row.get("availability") in {"yes", "maybe"}}
    )
    return VenueSearchCriteria(
        location=preferences.get("area"),
        party_size=preferences.get("number_of_people") or len(participant_names) or None,
        budget_min=preferences.get("budget_min"),
        budget_max=preferences.get("budget_max"),
        genre=preferences.get("preferred_food"),
        candidate_dates=dates,
        atmosphere=preferences.get("atmosphere"),
        start_time=preferences.get("start_time"),
        requirements=preferences.get("other_requirements"),
    )
