"""Dashboard route handlers for GET /api/v1/dashboard/summary."""

import logging

from aws_lambda_powertools.event_handler.api_gateway import Router

from src.api.async_trigger import trigger_async
from src.api.dal import garden_data
from src.api.middleware import require_garden

logger = logging.getLogger(__name__)

router = Router()


@router.get("/dashboard/summary")
@require_garden
def get_dashboard_summary(garden_id: str, user_id: str) -> dict:
    """GET /api/v1/dashboard/summary — return cached AI summary or trigger generation.

    If a cached summary exists, return it immediately (Req 11.1, 11.2).
    If not, fire an async session_open task to generate it, and return an
    empty-state response so the client can poll (Req 11.3, 11.4).

    Requirements: 11.1, 11.2, 11.3, 11.4
    """
    from src.api.router import app  # noqa: PLC0415

    item = garden_data.get_dashboard_summary(garden_id)

    if item is not None:
        return {
            "ai_summary_text": item.get("ai_summary_text"),
            "weather_chips": item.get("weather_chips", []),
            "generated_at": item.get("generated_at"),
        }

    # No cached summary — trigger async generation and return empty state
    try:
        request_id = getattr(app.current_event.request_context, "request_id", "dashboard")
    except AttributeError:
        request_id = "dashboard"

    try:
        trigger_async({"action": "session_open", "garden_id": garden_id}, request_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[request_id=%s] Failed to trigger session_open for garden_id=%s",
            request_id,
            garden_id,
        )

    return {
        "ai_summary_text": None,
        "weather_chips": [],
        "generated_at": None,
    }
