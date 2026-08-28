from app.models import Action, Decision, PreferenceUpdate
from app.services.venue_search import apply_search_override, build_search_criteria, is_venue_search_request


def test_explicit_shop_search_is_search_venue():
    decision = apply_search_override("店探して", Decision(action=Action.REPLY, reply_text="了解"))
    assert decision.action == Action.SEARCH_VENUE
    assert decision.search_required is True


def test_specific_yokohama_yakiniku_search_is_search_venue():
    decision = apply_search_override("横浜で焼肉屋探して", Decision(action=Action.REPLY))
    assert decision.action == Action.SEARCH_VENUE


def test_availability_does_not_trigger_web_search():
    assert is_venue_search_request("俺19日行ける") is False


def test_area_preference_does_not_trigger_web_search():
    assert is_venue_search_request("横浜がいい") is False


def test_saved_event_state_becomes_search_criteria(fresh_db):
    fresh_db.ensure_group("G1", "group")
    event_id = fresh_db.create_event("G1", "9月飲み")
    participant_id = fresh_db.ensure_participant(event_id, "U1", "田中")
    fresh_db.save_decision(
        event_id,
        participant_id,
        Decision(
            action=Action.REMEMBER_ONLY,
            preference_update=PreferenceUpdate(
                area="横浜",
                budget_max=5000,
                number_of_people=5,
                preferred_food="焼肉",
            ),
        ),
    )

    criteria = build_search_criteria(fresh_db.context("G1"))

    assert criteria.location == "横浜"
    assert criteria.budget_max == 5000
    assert criteria.party_size == 5
    assert criteria.genre == "焼肉"
