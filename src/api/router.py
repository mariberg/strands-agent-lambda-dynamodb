"""Main API router: registers all /api/v1/* route handlers via AWS Lambda Powertools APIGatewayRestResolver."""

import json
import logging
from typing import Any

from aws_lambda_powertools.event_handler import LambdaFunctionUrlResolver
from aws_lambda_powertools.event_handler import CORSConfig
from aws_lambda_powertools.utilities.typing import LambdaContext

from src.api.errors import ApiError, make_error_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Powertools app — CORS config satisfies Req 1.6 / 22.2 globally
# ---------------------------------------------------------------------------
app = LambdaFunctionUrlResolver(
    cors=CORSConfig(allow_origin="*", allow_headers=["Content-Type", "Authorization"])
)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(ApiError)
def handle_api_error(exc: ApiError):
    from aws_lambda_powertools.event_handler.api_gateway import Response
    request_id = _get_request_id()
    logger.warning("[request_id=%s] ApiError %s: %s", request_id, exc.error, exc.message)
    return Response(
        status_code=exc.status_code,
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
        body=json.dumps({
            "error": exc.error,
            "message": exc.message,
            "request_id": request_id,
        }),
    )


@app.exception_handler(Exception)
def handle_unhandled(exc: Exception):
    from aws_lambda_powertools.event_handler.api_gateway import Response
    request_id = _get_request_id()
    logger.exception("[request_id=%s] Unhandled exception", request_id)
    return Response(
        status_code=500,
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
        body=json.dumps({
            "error": "internal_error",
            "message": "An unexpected error occurred.",
            "request_id": request_id,
        }),
    )


def _get_request_id() -> str:
    """Safely extract the API Gateway request ID from the current event context."""
    try:
        return app.current_event.request_context.request_id or "unknown"
    except AttributeError:
        return "unknown"


# ---------------------------------------------------------------------------
# Route registration — import each route module and include its router
# ---------------------------------------------------------------------------
# Imports are deferred to avoid circular imports with middleware.py which
# itself imports `app` from this module.

def _register_routes():
    from src.api.routes import plants, action_logs, dashboard, agent  # noqa: PLC0415
    prefix = "/api/v1"
    app.include_router(plants.router, prefix=prefix)
    app.include_router(action_logs.router, prefix=prefix)
    app.include_router(dashboard.router, prefix=prefix)
    app.include_router(agent.router, prefix=prefix)


_register_routes()


# ---------------------------------------------------------------------------
# Async task dispatcher — called when event contains "async_task" key
# ---------------------------------------------------------------------------

def handle_async_task(payload: dict[str, Any]) -> dict:
    """Route an async task payload to the agent logic.

    Entry point for fire-and-forget Lambda self-invocations from trigger_async().
    """
    action = payload.get("action", "")
    logger.info("Handling async task: action=%s", action)

    if action == "session_open":
        _handle_session_open(payload)
    else:
        logger.warning("Unknown async task action: %s", action)

    return {"status": "ok"}


def _handle_session_open(payload: dict) -> None:
    """Refresh dashboard summary for the garden via the Strands agent."""
    garden_id = payload.get("garden_id")
    logger.info("session_open: garden_id=%s", garden_id)
    if not garden_id:
        logger.error("session_open: missing garden_id in async payload")
        return
    from src.api.agent_runner import run_session_open  # noqa: PLC0415
    run_session_open(garden_id)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: LambdaContext) -> dict:
    """AWS Lambda entry point for the Gardening API Handler function.

    Handles two event types:
    - API Gateway proxy events (routed to Powertools APIGatewayRestResolver)
    - Async task events (containing "async_task" key, fire-and-forget from trigger_async)
    """
    if "async_task" in event:
        return handle_async_task(event["async_task"])
    return app.resolve(event, context)
