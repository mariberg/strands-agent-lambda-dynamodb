"""Action log route handlers.

  GET  /api/v1/plants/{instance_id}/action-logs  — list logs for a plant (paginated)
  POST /api/v1/plants/{instance_id}/action-logs  — create a new log entry
"""

from aws_lambda_powertools.event_handler.api_gateway import Router

from src.api.dal import garden_data
from src.api.enums import LoggedBy
from src.api.errors import ApiError
from src.api.middleware import require_garden
from src.api.services import garden_service

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

    body = app.current_event.json_body or {}

    # Validation, item format and the write are shared with the agent's
    # create_action_log tool. Invalid input raises ApiError, which the router
    # turns into a 4xx response.
    created = garden_service.create_action_log(
        garden_id,
        instance_id,
        action_type=body.get("action_type"),
        title=body.get("title"),
        notes=body.get("notes"),
        severity=body.get("severity"),
        logged_by=body.get("logged_by") or LoggedBy.user.value,
        occurred_at=body.get("occurred_at"),
        is_draft=body.get("is_draft", False),
        resolved=body.get("resolved", False),
    )

    import json  # noqa: PLC0415
    from aws_lambda_powertools.event_handler.api_gateway import Response  # noqa: PLC0415

    response_item = {
        **created.item,
        "action_id": created.action_id,
        "instance_id": created.instance_id,
        "occurred_at": created.occurred_at,
    }
    return Response(
        status_code=201,
        content_type="application/json",
        body=json.dumps(_build_log_response(response_item)),
    )
