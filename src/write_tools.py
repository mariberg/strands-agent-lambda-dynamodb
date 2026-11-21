"""
Garden action recording tool for the Strands agent.

Design guardrails baked in here
--------------------------------
1. The @tool schema deliberately omits user_id and garden_id.
   Ownership is resolved from a per-request ContextVar set by lambda_handler
   *before* the agent runs — not from anything the LLM supplies.

2. validate_garden_action() is the single validation function for garden
   actions.  Both the agent-tool path (record_garden_action) and any future
   REST handler that exposes this operation directly must call it, so that
   tightening a rule in one place tightens it everywhere.
"""

import re
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, FrozenSet, Optional

from strands import tool  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ACTION_TYPES: FrozenSet[str] = frozenset(
    {"watered", "fertilized", "pruned", "repotted", "harvested", "treated"}
)

# ---------------------------------------------------------------------------
# Per-request authorization context
# ---------------------------------------------------------------------------
# lambda_handler resolves the authenticated user's plant list from DynamoDB
# and stores it here before handing off to the agent.  The tool reads it at
# call time.  The LLM has no mechanism to write to this variable.

_authorized_plant_ids: ContextVar[Optional[FrozenSet[str]]] = ContextVar(
    "authorized_plant_ids", default=None
)


def set_authorized_plants(plant_ids: FrozenSet[str]) -> None:
    """Bind the set of plant IDs the authenticated user owns to the current context."""
    _authorized_plant_ids.set(plant_ids)


def clear_authorized_plants() -> None:
    """Remove the authorization context (call at end of request / in test teardown)."""
    _authorized_plant_ids.set(None)


# ---------------------------------------------------------------------------
# Shared validation — the single source of truth for garden-action inputs
# ---------------------------------------------------------------------------


def validate_garden_action(
    action_type: str, plant_id: str, date: str
) -> tuple[bool, str]:
    """
    Validate the inputs for a garden action.

    Returns:
        (is_valid, error_message) — error_message is "" when is_valid is True.
    """
    # --- action_type ---
    if not isinstance(action_type, str) or not action_type.strip():
        return False, "'action_type' must be a non-empty string."
    if action_type.strip() not in VALID_ACTION_TYPES:
        return False, (
            f"Invalid action_type '{action_type}'. "
            f"Must be one of: {sorted(VALID_ACTION_TYPES)}."
        )

    # --- plant_id ---
    if not isinstance(plant_id, str) or not plant_id.strip():
        return False, "'plant_id' must be a non-empty string."
    if not re.match(r"^[a-zA-Z0-9_-]+$", plant_id.strip()):
        return False, "'plant_id' contains invalid characters."

    # --- date (YYYY-MM-DD only) ---
    if not isinstance(date, str) or not date.strip():
        return False, "'date' must be a non-empty string."
    try:
        datetime.strptime(date.strip(), "%Y-%m-%d")
    except ValueError:
        return False, f"Invalid date '{date}'. Expected format: YYYY-MM-DD."

    return True, ""


def check_plant_authorization(plant_id: str) -> tuple[bool, str]:
    """
    Check whether the current request is allowed to write to plant_id.

    The authorization context must have been set by lambda_handler before
    the agent was invoked.
    """
    authorized = _authorized_plant_ids.get()
    if authorized is None:
        return False, "No authorization context set for this request."
    stripped = plant_id.strip()
    if stripped not in authorized:
        return False, (
            f"Plant '{stripped}' does not exist or does not belong to your garden."
        )
    return True, ""


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@tool
def record_garden_action(
    action_type: str,
    plant_id: str,
    date: str,
    notes: str = "",
) -> Dict[str, Any]:
    """
    Record a gardening action performed on a specific plant.

    Args:
        action_type: Type of action. One of: watered, fertilized, pruned,
                     repotted, harvested, treated.
        plant_id:    ID of the plant the action was performed on.
        date:        Date the action was performed, in YYYY-MM-DD format.
        notes:       Optional free-text notes about the action.

    Returns:
        Confirmation dict on success, or a dict with an "error" key on failure.
    """
    is_valid, error_msg = validate_garden_action(action_type, plant_id, date)
    if not is_valid:
        return {"error": error_msg}

    is_authorized, auth_error = check_plant_authorization(plant_id)
    if not is_authorized:
        return {"error": auth_error}

    return {
        "status": "recorded",
        "action_type": action_type.strip(),
        "plant_id": plant_id.strip(),
        "date": date.strip(),
        "notes": notes,
    }
