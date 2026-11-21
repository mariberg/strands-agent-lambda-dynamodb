"""Agent route handlers.

  POST   /api/v1/agent/session-open — trigger an async Strands agent run for the garden
  POST   /api/v1/agent/chat         — synchronous conversational turn with the garden agent
  DELETE /api/v1/agent/chat         — reset the conversation history
"""

import json
import logging

from aws_lambda_powertools.event_handler.api_gateway import Response, Router

from src.api.middleware import require_garden

logger = logging.getLogger(__name__)
router = Router()


@router.post("/agent/session-open")
@require_garden
def session_open(garden_id: str, user_id: str) -> tuple:
    """POST /api/v1/agent/session-open — kick off an async agent session.

    The agent queries the garden state, refreshes the dashboard summary, and
    can write action logs or update plant statuses. The request returns
    immediately with 202; the agent runs fire-and-forget via Lambda self-invoke.
    """
    from src.api.router import _get_request_id  # noqa: PLC0415
    from src.api.async_trigger import trigger_async  # noqa: PLC0415

    request_id = _get_request_id()
    try:
        trigger_async(
            {"action": "session_open", "garden_id": garden_id},
            request_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return {"status": "accepted"}, 202


@router.post("/agent/chat")
@require_garden
def chat(garden_id: str, user_id: str) -> Response:
    """POST /api/v1/agent/chat — synchronous conversational turn with the garden agent.

    Loads the existing single-session conversation history for the garden,
    runs the agent with the user's message, saves the updated history, and
    returns the agent's reply.

    Request body:
        message (str, required): The user's message.

    Response:
        reply (str): The agent's response.
    """
    from src.api.router import app  # noqa: PLC0415
    from src.api.agent_runner import run_chat  # noqa: PLC0415
    from src.api.errors import ApiError  # noqa: PLC0415

    body = app.current_event.json_body or {}
    message = body.get("message", "").strip()
    if not message:
        raise ApiError(400, "missing_field", "'message' is required.")

    reply = run_chat(garden_id, message)

    return Response(
        status_code=200,
        content_type="application/json",
        body=json.dumps({"reply": reply}),
    )


@router.delete("/agent/chat")
@require_garden
def reset_chat(garden_id: str, user_id: str) -> Response:
    """DELETE /api/v1/agent/chat — clear the conversation history for this garden.

    Removes the saved chat session so the next POST /agent/chat starts fresh.
    """
    from src.api.dal import garden_data  # noqa: PLC0415

    garden_data.delete_chat_session(garden_id)
    return Response(
        status_code=200,
        content_type="application/json",
        body=json.dumps({"status": "conversation reset"}),
    )
