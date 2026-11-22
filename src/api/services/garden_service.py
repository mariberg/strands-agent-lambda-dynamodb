"""Shared write operations for plants and action logs.

Both the REST routes (src/api/routes) and the agent tools
(src/api/agent_runner.py) call these functions. Validation, item format and
the database write all live here once, so the two paths can't drift apart.

Callers decide which garden to write to. REST routes get it from
require_garden; agent tools get it from the authorized request context.
The model never supplies it.

Invalid input raises ApiError. REST routes let it propagate to the router's
error handler, which returns a 4xx. Agent tools catch it and hand the message
back to the model, which can retry or explain the problem to the user.
Nothing is silently dropped or corrected.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.api.dal import garden_data
from src.api.dal import species as species_dal
from src.api.enums import ActionType, LoggedBy, Severity, validate_enum
from src.api.errors import ApiError


@dataclass(frozen=True)
class CreatedPlant:
    instance_id: str
    item: dict       # exactly what was written (before DAL key fields)
    species: dict    # species record, so callers don't need a second lookup


@dataclass(frozen=True)
class CreatedActionLog:
    action_id: str
    instance_id: str
    occurred_at: str
    item: dict       # exactly what was written (before DAL key fields)
    plant: dict      # plant record, e.g. for its nickname


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_timestamp(field_name: str, value) -> str:
    """Accept an ISO 8601 date or timestamp string; reject anything else."""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_param", f"'{field_name}' must be an ISO 8601 string.")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(
            400,
            "invalid_param",
            f"'{field_name}' must be an ISO 8601 timestamp (e.g. 2026-06-01T09:00:00Z), got '{value}'.",
        )
    return value


def _validate_bool(field_name: str, value) -> bool:
    if not isinstance(value, bool):
        raise ApiError(400, "invalid_param", f"'{field_name}' must be true or false.")
    return value


def _without_none(item: dict) -> dict:
    """Store optional fields only when they have a value. One format for every caller."""
    return {k: v for k, v in item.items() if v is not None}


def create_plant(
    garden_id: str,
    species_id: Optional[str],
    nickname: Optional[str],
    emoji: Optional[str] = None,
    garden_location: Optional[str] = None,
    planted_at: Optional[str] = None,
) -> CreatedPlant:
    """Validate and write a new plant to garden_id."""
    missing = [name for name, value in (("species_id", species_id), ("nickname", nickname)) if not value]
    if missing:
        raise ApiError(400, "missing_field", f"Required field(s) missing: {missing}.")

    species_data = species_dal.get_species(species_id)
    if not species_data:
        raise ApiError(
            400,
            "not_found",
            f"Species '{species_id}' not found. Species IDs are lowercase hyphenated "
            "scientific names, e.g. 'rosa' or 'solanum-lycopersicum'.",
        )

    now = _now_iso()
    item = _without_none({
        "species_id": species_id,
        "nickname": nickname,
        "emoji": emoji or None,
        "garden_location": garden_location or None,
        "planted_at": _validate_timestamp("planted_at", planted_at) if planted_at else now,
        "status": "thriving",
        "updated_at": now,
    })

    instance_id = str(uuid.uuid4())
    garden_data.put_plant(garden_id, instance_id, item)
    return CreatedPlant(instance_id=instance_id, item=item, species=species_data)


def create_action_log(
    garden_id: str,
    instance_id: str,
    action_type: Optional[str],
    title: Optional[str] = None,
    notes: Optional[str] = None,
    severity: Optional[str] = None,
    logged_by: str = LoggedBy.user.value,
    occurred_at: Optional[str] = None,
    is_draft: bool = False,
    resolved: bool = False,
) -> CreatedActionLog:
    """Validate and write an action log for a plant in garden_id.

    The plant must exist in garden_id. A plant ID from another garden is
    treated the same as one that doesn't exist.
    """
    plant = garden_data.get_plant(garden_id, instance_id)
    if not plant:
        raise ApiError(404, "not_found", f"Plant '{instance_id}' not found in this garden.")

    if not action_type:
        raise ApiError(400, "missing_field", "'action_type' is required.")

    item = _without_none({
        "action_type": validate_enum("action_type", action_type, ActionType),
        "title": title or None,
        "notes": notes or None,
        "severity": validate_enum("severity", severity, Severity) if severity else None,
        "logged_by": validate_enum("logged_by", logged_by, LoggedBy),
        "is_draft": _validate_bool("is_draft", is_draft),
        "image_refs": [],
        "resolved": _validate_bool("resolved", resolved),
    })
    occurred_at = _validate_timestamp("occurred_at", occurred_at) if occurred_at else _now_iso()

    action_id = str(uuid.uuid4())
    garden_data.put_log(
        garden_id=garden_id,
        instance_id=instance_id,
        occurred_at=occurred_at,
        action_id=action_id,
        item=item,
    )
    return CreatedActionLog(
        action_id=action_id,
        instance_id=instance_id,
        occurred_at=occurred_at,
        item=item,
        plant=plant,
    )
