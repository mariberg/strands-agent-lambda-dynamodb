"""Action log route handlers.

  GET  /api/v1/plants/{instance_id}/action-logs  — list logs for a plant (paginated)
  POST /api/v1/plants/{instance_id}/action-logs  — create a new log entry
"""

import uuid
from datetime import datetime, timezone

from aws_lambda_powertools.event_handler.api_gateway import Router

from src.api.dal import garden_data
from src.api.enums import ActionType, LoggedBy, Severity, validate_enum
from src.api.errors import ApiError
from src.api.middleware import require_garden

router = Router()


def _build_log_response(item: dict) -> dict:
    """Map a raw DynamoDB item to the canonical action log response shape."""
    return {
        "action_id": item.get("action_id"),
        "instance_id": item.get("instance_id"),
        "action_type": item.get("action_type"),
        "title": item.get("title"),
        "notes": item.get("notes"),
        "severity": item.get("severity"),
        "logged_by": item.get("logged_by"),
        "occurred_at": item.get("occurred_at"),
        "is_draft": item.get("is_draft"),
        "image_refs": item.get("image_refs") or [],
        "resolved": item.get("resolved"),
    }


@router.get("/plants/<instance_id>/action-logs")
@require_garden
def list_plant_action_logs(instance_id: str, garden_id: str, user_id: str) -> dict:
    """GET /api/v1/plants/{instance_id}/action-logs — paginated logs for one plant.

    Query parameters (optional): limit (default 20), offset (default 0)
    """
    from src.api.router import app  # noqa: PLC0415

    plant = garden_data.get_plant(garden_id, instance_id)
    if not plant:
        raise ApiError(404, "not_found", f"Plant '{instance_id}' not found in this garden.")

    raw_limit = app.current_event.get_query_string_value("limit") or "20"
    raw_offset = app.current_event.get_query_string_value("offset") or "0"

    try:
        limit = max(1, int(raw_limit))
    except ValueError:
        raise ApiError(400, "invalid_param", "'limit' must be an integer.")
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        raise ApiError(400, "invalid_param", "'offset' must be an integer.")

    result = garden_data.list_logs_paginated(garden_id, instance_id, limit, offset)

    return {
        "entries": [_build_log_response(item) for item in result["items"]],
        "total": result["total"],
        "limit": limit,
        "offset": offset,
    }


@router.post("/plants/<instance_id>/action-logs")
@require_garden
def create_plant_action_log(instance_id: str, garden_id: str, user_id: str) -> dict:
    """POST /api/v1/plants/{instance_id}/action-logs — create a new action log entry.

    Required body field: action_type
    Optional body fields: title, notes, severity, logged_by, occurred_at, is_draft, resolved
    """
    from src.api.router import app  # noqa: PLC0415

    plant = garden_data.get_plant(garden_id, instance_id)
    if not plant:
        raise ApiError(404, "not_found", f"Plant '{instance_id}' not found in this garden.")

    body = app.current_event.json_body or {}

    raw_action_type = body.get("action_type")
    if not raw_action_type:
        raise ApiError(400, "missing_field", "'action_type' is required.")
    action_type = validate_enum("action_type", raw_action_type, ActionType)

    raw_severity = body.get("severity")
    severity = validate_enum("severity", raw_severity, Severity) if raw_severity else None

    raw_logged_by = body.get("logged_by")
    logged_by = validate_enum("logged_by", raw_logged_by, LoggedBy) if raw_logged_by else None

    action_id = str(uuid.uuid4())
    occurred_at = body.get("occurred_at") or datetime.now(timezone.utc).isoformat()

    item = {
        "action_type": action_type,
        "title": body.get("title"),
        "notes": body.get("notes"),
        "severity": severity,
        "logged_by": logged_by,
        "is_draft": body.get("is_draft", False),
        "image_refs": [],
        "resolved": body.get("resolved", False),
    }

    garden_data.put_log(
        garden_id=garden_id,
        instance_id=instance_id,
        occurred_at=occurred_at,
        action_id=action_id,
        item=item,
    )

    import json  # noqa: PLC0415
    from aws_lambda_powertools.event_handler.api_gateway import Response  # noqa: PLC0415

    response_item = {**item, "action_id": action_id, "instance_id": instance_id, "occurred_at": occurred_at}
    return Response(
        status_code=201,
        content_type="application/json",
        body=json.dumps(_build_log_response(response_item)),
    )
