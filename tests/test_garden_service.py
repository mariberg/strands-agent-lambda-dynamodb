"""
Tests for src/api/services/garden_service.py.

This is the one place plant and action-log writes are validated and built.
Both the REST routes and the agent tools call it (see
test_garden_tool_guardrails.py::TestSharedWritePath), so every rule here
applies to both paths.

Rule for bad input: reject with ApiError and a clear message. Never write a
partial or "corrected" item.
"""

from unittest.mock import patch

import pytest

from src.api.dal import garden_data, species as species_dal
from src.api.errors import ApiError
from src.api.services import garden_service

GARDEN = "demo-garden-1"
NOW = "2026-06-01T09:00:00+00:00"


@pytest.fixture
def dal():
    """Fake DAL: one plant (rose-01) in GARDEN, one species (rosa)."""
    with patch.object(garden_data, "get_plant",
                      side_effect=lambda g, i: {"instance_id": i, "nickname": "Big Rose"}
                      if (g, i) == (GARDEN, "rose-01") else None) as get_plant, \
         patch.object(garden_data, "put_plant") as put_plant, \
         patch.object(garden_data, "put_log") as put_log, \
         patch.object(species_dal, "get_species",
                      side_effect=lambda s: {"common_name": "Rose"} if s == "rosa" else None), \
         patch.object(garden_service, "_now_iso", return_value=NOW):
        yield {"get_plant": get_plant, "put_plant": put_plant, "put_log": put_log}


def assert_rejected(call, status: int, fragment: str):
    with pytest.raises(ApiError) as exc_info:
        call()
    assert exc_info.value.status_code == status
    assert fragment in exc_info.value.message
    return exc_info.value


# ---------------------------------------------------------------------------
# create_plant
# ---------------------------------------------------------------------------

class TestCreatePlant:

    def test_minimal_plant_item_format(self, dal):
        created = garden_service.create_plant(GARDEN, "rosa", "Rose")
        garden_id, instance_id, item = dal["put_plant"].call_args.args
        assert garden_id == GARDEN
        assert instance_id == created.instance_id
        # Optional fields that weren't given are absent, not stored as null.
        assert item == {
            "species_id": "rosa",
            "nickname": "Rose",
            "planted_at": NOW,
            "status": "thriving",
            "updated_at": NOW,
        }

    def test_optional_fields_stored_when_given(self, dal):
        garden_service.create_plant(GARDEN, "rosa", "Rose", emoji="🌹",
                                    garden_location="South bed", planted_at="2026-04-01")
        item = dal["put_plant"].call_args.args[2]
        assert item["emoji"] == "🌹"
        assert item["garden_location"] == "South bed"
        assert item["planted_at"] == "2026-04-01"

    @pytest.mark.parametrize("species_id, nickname, missing", [
        (None, "Rose", "species_id"),
        ("rosa", None, "nickname"),
        ("", "", "species_id"),
    ])
    def test_missing_required_field(self, dal, species_id, nickname, missing):
        assert_rejected(lambda: garden_service.create_plant(GARDEN, species_id, nickname), 400, missing)
        dal["put_plant"].assert_not_called()

    def test_unknown_species(self, dal):
        assert_rejected(lambda: garden_service.create_plant(GARDEN, "lemon-tree", "Lemon"), 400, "not found")
        dal["put_plant"].assert_not_called()

    @pytest.mark.parametrize("bad", ["yesterday", "01/04/2026", "2026-13-01", 20260401])
    def test_malformed_planted_at(self, dal, bad):
        assert_rejected(lambda: garden_service.create_plant(GARDEN, "rosa", "Rose", planted_at=bad),
                        400, "planted_at")
        dal["put_plant"].assert_not_called()


# ---------------------------------------------------------------------------
# create_action_log
# ---------------------------------------------------------------------------

class TestCreateActionLog:

    def test_minimal_log_item_format(self, dal):
        created = garden_service.create_action_log(GARDEN, "rose-01", "watered")
        kwargs = dal["put_log"].call_args.kwargs
        assert kwargs["garden_id"] == GARDEN
        assert kwargs["instance_id"] == "rose-01"
        assert kwargs["occurred_at"] == NOW
        assert kwargs["action_id"] == created.action_id
        assert kwargs["item"] == {
            "action_type": "watered",
            "logged_by": "user",
            "is_draft": False,
            "image_refs": [],
            "resolved": False,
        }

    def test_optional_fields_stored_when_given(self, dal):
        garden_service.create_action_log(
            GARDEN, "rose-01", "issue_found", title="Aphids", notes="On new growth",
            severity="moderate", logged_by="agent", occurred_at="2026-05-30T18:00:00Z",
        )
        kwargs = dal["put_log"].call_args.kwargs
        assert kwargs["occurred_at"] == "2026-05-30T18:00:00Z"
        assert kwargs["item"]["severity"] == "moderate"
        assert kwargs["item"]["logged_by"] == "agent"
        assert kwargs["item"]["title"] == "Aphids"

    def test_returns_plant_record(self, dal):
        created = garden_service.create_action_log(GARDEN, "rose-01", "watered")
        assert created.plant["nickname"] == "Big Rose"

    def test_plant_not_in_garden(self, dal):
        assert_rejected(lambda: garden_service.create_action_log(GARDEN, "orchid-07", "watered"),
                        404, "not found")
        dal["put_log"].assert_not_called()

    def test_plant_lookup_uses_given_garden(self, dal):
        """A plant ID is only valid within the garden the caller passed in."""
        with pytest.raises(ApiError):
            garden_service.create_action_log("demo-garden-4", "rose-01", "watered")
        dal["get_plant"].assert_called_once_with("demo-garden-4", "rose-01")

    @pytest.mark.parametrize("field, kwargs", [
        ("action_type", {"action_type": None}),
        ("action_type", {"action_type": "sprayed"}),
        ("action_type", {"action_type": "WATERED"}),
        ("severity", {"action_type": "issue_found", "severity": "catastrophic"}),
        ("logged_by", {"action_type": "watered", "logged_by": "robot"}),
        ("occurred_at", {"action_type": "watered", "occurred_at": "last tuesday"}),
        ("is_draft", {"action_type": "watered", "is_draft": "yes"}),
        ("resolved", {"action_type": "watered", "resolved": 1}),
    ])
    def test_invalid_input_rejected_not_dropped(self, dal, field, kwargs):
        assert_rejected(lambda: garden_service.create_action_log(GARDEN, "rose-01", **kwargs), 400, field)
        dal["put_log"].assert_not_called()

    def test_plant_check_runs_before_field_checks(self, dal):
        """An unknown plant is reported as 404 even when other fields are also bad."""
        assert_rejected(lambda: garden_service.create_action_log(GARDEN, "orchid-07", "sprayed"),
                        404, "not found")
