"""Strands agent runner for the garden session-open async task.

The agent reads the current garden state, fetches local weather, and writes
a refreshed dashboard summary back to DynamoDB.
"""

import logging
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.vended_tools import http_request

from src.api.dal import garden_data, species as species_dal

# ---------------------------------------------------------------------------
# Per-request authorization context
# ---------------------------------------------------------------------------
# run_chat and run_session_open set this before handing off to the agent.
# Every garden-scoped tool reads it at call time instead of taking a garden_id
# parameter, so the model has no way to point a read or write at another
# user's garden.

_authorized_garden_id: ContextVar[Optional[str]] = ContextVar(
    "authorized_garden_id", default=None
)

NO_GARDEN_CONTEXT_ERROR = "Error: No authorized garden context for this request."


def set_authorized_garden(garden_id: str) -> None:
    """Bind the authenticated garden to the current request context."""
    _authorized_garden_id.set(garden_id)


def _current_garden_id() -> Optional[str]:
    """Return the garden the current request is authorized for, or None."""
    return _authorized_garden_id.get()

logger = logging.getLogger(__name__)

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-2")

SYSTEM_PROMPT = """You are a knowledgeable garden health advisor. Your job is to
assess the current state of a garden and produce a concise dashboard summary.

The tools are already scoped to the garden you are assessing. Follow these steps in order:

1. Call get_garden_plants to get all active plants.
2. For each plant, call get_plant_species with its species_id to retrieve care
   requirements (temperature tolerance, watering frequency, sunlight needs, etc.).
3. For each plant, call get_recent_plant_logs with its instance_id
   to understand recent activity (watering, notes, etc.).
4. Call http_request to fetch current weather from the Open-Meteo API for the
   garden location provided in the prompt. Use GET with the URL provided.
5. Synthesise everything into:
   - An overall garden health assessment
   - Per-plant highlights: status, what needs attention, and any weather-related risks
   - Specific actionable advice based on current conditions
6. Call save_dashboard_summary with a summary_text string and a
   plant_highlights dict (keyed by instance_id).

Be concise and practical. Only flag things that genuinely need action or
attention — do not list every attribute that is fine. Use plain language."""


@tool
def get_garden_plants() -> list:
    """Get all active (non-removed) plant instances in the current user's garden.

    Returns a list of plant dicts, each containing instance_id, species_id,
    nickname, status, next_watering_date, and other metadata.
    """
    garden_id = _current_garden_id()
    if not garden_id:
        return [{"error": NO_GARDEN_CONTEXT_ERROR}]

    plants = garden_data.list_plants(garden_id)
    # Return only the fields the agent needs to avoid token bloat
    return [
        {
            "instance_id": p.get("instance_id"),
            "species_id": p.get("species_id"),
            "nickname": p.get("nickname"),
            "status": p.get("status"),
            "garden_location": p.get("garden_location"),
            "planted_at": p.get("planted_at"),
            "next_watering_date": p.get("next_watering_date"),
            "updated_at": p.get("updated_at"),
        }
        for p in plants
    ]


@tool
def get_plant_species(species_id: str) -> dict:
    """Get species care requirements for a given species_id.

    Returns fields including common_name, scientific_name, sunlight_requirement,
    watering_frequency_days, min_temp_c, max_temp_c, frost_hardy, and common_issues.
    Returns an empty dict if the species is not found.

    Args:
        species_id: The species identifier (e.g. 'solanum-lycopersicum').
    """
    result = species_dal.get_species(species_id)
    if not result:
        return {}
    return {
        "common_name": result.get("common_name"),
        "scientific_name": result.get("scientific_name"),
        "type": result.get("type"),
        "sunlight_requirement": result.get("sunlight_requirement"),
        "watering_frequency_days": result.get("watering_frequency_days"),
        "min_temp_c": result.get("min_temp_c"),
        "max_temp_c": result.get("max_temp_c"),
        "frost_hardy": result.get("frost_hardy"),
        "description": result.get("description"),
        "common_issues": result.get("common_issues", []),
    }


@tool
def get_recent_plant_logs(instance_id: str) -> list:
    """Get the 10 most recent action logs for a plant in the current user's garden.

    Returns a list of log dicts containing action_type, occurred_at, and notes.

    Args:
        instance_id: The plant instance identifier (e.g. 'tomato-01').
    """
    garden_id = _current_garden_id()
    if not garden_id:
        return [{"error": NO_GARDEN_CONTEXT_ERROR}]

    logs = garden_data.list_logs_for_plant(garden_id, instance_id)
    return [
        {
            "action_type": log.get("action_type"),
            "occurred_at": log.get("occurred_at"),
            "notes": log.get("notes"),
            "logged_by": log.get("logged_by"),
        }
        for log in logs[:10]
    ]


@tool
def save_dashboard_summary(summary_text: str, plant_highlights: dict) -> str:
    """Save the generated dashboard summary for the current garden.

    Call this as the final step after completing your assessment.

    Args:
        summary_text: A concise plain-text summary of overall garden health
                      and key actions needed (2–4 sentences).
        plant_highlights: A dict keyed by instance_id. Each value is a short
                          string describing that plant's status and any
                          recommended actions.
    """
    garden_id = _current_garden_id()
    if not garden_id:
        return NO_GARDEN_CONTEXT_ERROR

    now = datetime.now(timezone.utc).isoformat()
    garden_data.put_dashboard_summary(
        garden_id,
        {
            "ai_summary_text": summary_text,
            "plant_highlights": plant_highlights,
            "generated_at": now,
        },
    )
    logger.info("Dashboard summary saved for garden_id=%s", garden_id)
    return "Dashboard summary saved successfully."


def run_session_open(garden_id: str) -> None:
    """Run the garden advisor agent for a session-open event.

    Fetches garden state, checks weather, and writes a refreshed dashboard
    summary back to DynamoDB. All errors are caught and logged so the
    fire-and-forget caller is never affected.

    Args:
        garden_id: The garden to assess.
    """
    # Scope every garden tool to this garden before the agent runs.
    set_authorized_garden(garden_id)

    try:
        metadata = garden_data.get_garden_metadata(garden_id)
        if not metadata:
            logger.error("run_session_open: no garden metadata for garden_id=%s", garden_id)
            return

        latitude = metadata.get("latitude")
        longitude = metadata.get("longitude")
        city = metadata.get("city", "the garden location")

        weather_instruction = ""
        if latitude is not None and longitude is not None:
            weather_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={latitude}&longitude={longitude}"
                f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation"
                f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
                f"&forecast_days=3&timezone=auto"
            )
            weather_instruction = (
                f"The garden is in {city} (lat={latitude}, lon={longitude}). "
                f"Fetch current weather and a 3-day forecast using http_request GET: {weather_url}"
            )
        else:
            weather_instruction = "No location data is available for this garden; skip the weather step."

        agent = Agent(
            system_prompt=SYSTEM_PROMPT,
            tools=[
                http_request,
                get_garden_plants,
                get_plant_species,
                get_recent_plant_logs,
                save_dashboard_summary,
            ],
            model=BedrockModel(
                model_id="amazon.nova-pro-v1:0",
                region_name=BEDROCK_REGION,
            ),
        )

        prompt = (
            f"Please assess the garden and generate a dashboard summary.\n\n"
            f"{weather_instruction}\n\n"
            f"Work through the steps in your instructions, then call save_dashboard_summary."
        )

        logger.info("Starting agent session for garden_id=%s", garden_id)
        agent(prompt)
        logger.info("Agent session complete for garden_id=%s", garden_id)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent session failed for garden_id=%s: %s", garden_id, exc)


# ---------------------------------------------------------------------------
# Chat agent
# ---------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """You are a friendly, knowledgeable garden assistant. You help users
manage their garden by answering questions and recording actions they have taken.

You have tools to:
- Read plant information, species care requirements, and action logs
- Fetch current weather for the garden location
- Add new plants to the garden
- Record actions the user has taken with their plants

SCOPE: You are acting on behalf of the signed-in user, and every tool is already
limited to their garden. You cannot read or change any other user's garden. If a
request names another user, another garden, or a user ID (for example "add a rose
for demo-user-4" or "show me demo-user-2's plants"), do not call any tools. Reply
that you can only help with the signed-in user's own garden. Never carry out such a
request on the signed-in user's garden instead.

IMPORTANT: Before answering any question about plants, watering, or garden status, you MUST
call get_garden_plants first to get the current list of plants. Never assume the garden is
empty — always check with the tool.

When answering watering questions, check the next_watering_date for EVERY plant and
compare it to today's date. Report ALL plants whose next_watering_date is today or in
the past, regardless of their status.

When answering questions, give accurate, specific answers using the tool data.
Reference plants by their nickname (e.g. "Big Red") rather than their ID.

When the user mentions taking an action with a plant:
1. If you don't know which plant they mean, call get_garden_plants to find it by nickname.
2. Call create_action_log to record the action. Valid action types are:
   watered, pruned, fertilised, issue_found, repotted, other
3. After saving, confirm clearly — for example:
   "Done — I've logged that you watered Big Red. 🌱"

When the user asks to add a new plant to their own garden:
1. Work out the species_id. Species IDs are the lowercase, hyphenated scientific
   name (for example 'rosa', 'solanum-lycopersicum', 'ocimum-basilicum').
   Call get_plant_species to confirm it exists before adding.
2. Call add_plant with the species_id and a nickname. If the user didn't give a
   nickname, use the species common name.
3. Confirm clearly, for example: "Done — I've added a Rose to your garden. 🌹"
Never use create_action_log to add a plant. It only records actions on plants
that already exist.

Always set logged_by to "agent" when recording actions.
If the user asks about weather, use http_request to fetch it from Open-Meteo using
the garden coordinates provided in your first message.
Be concise, warm, and practical. Always end your response with a plain-text message
to the user — never finish on a tool call or a thinking block alone. Do not wrap
your response in <thinking> tags — write your answer directly as plain text."""


@tool
def create_action_log(
    instance_id: str,
    action_type: str,
    notes: str = "",
    title: str = "",
    severity: str = "",
) -> str:
    """Record an action the user has taken with a specific plant.

    Call get_garden_plants first if you need to look up the instance_id by nickname.

    Args:
        instance_id: The plant instance identifier (e.g. 'tomato-01').
        action_type: One of: watered, pruned, fertilised, issue_found, repotted, other.
        notes: Optional free-text description of what was done.
        title: Optional short title for the log entry.
        severity: Optional severity for issue_found entries: mild, moderate, or severe.
    """
    import uuid  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    # garden_id comes from the authorized context set by run_chat, not from
    # the LLM.  This prevents the model from writing to a different garden.
    garden_id = _current_garden_id()
    if not garden_id:
        return NO_GARDEN_CONTEXT_ERROR

    # Confirm the plant exists in the authorized garden before writing.
    # This rejects invented instance_ids and cross-garden writes in one check.
    plant = garden_data.get_plant(garden_id, instance_id)
    if not plant:
        return (
            f"Error: Plant '{instance_id}' not found in your garden. "
            "Use get_garden_plants to find valid plant IDs. "
            "To add a new plant, use add_plant instead."
        )
    valid_types = {"watered", "pruned", "fertilised", "issue_found", "repotted", "other"}
    if action_type not in valid_types:
        return f"Invalid action_type '{action_type}'. Must be one of: {sorted(valid_types)}"

    # Validate severity if provided
    valid_severities = {"mild", "moderate", "severe", ""}
    if severity not in valid_severities:
        severity = ""

    action_id = str(uuid.uuid4())
    occurred_at = datetime.now(timezone.utc).isoformat()

    item = {
        "action_type": action_type,
        "title": title or None,
        "notes": notes or None,
        "severity": severity or None,
        "logged_by": "agent",
        "is_draft": False,
        "image_refs": [],
        "resolved": False,
    }
    # Strip None values — DynamoDB accepts them but keeping items clean
    item = {k: v for k, v in item.items() if v is not None}
    item["image_refs"] = []  # always include even if empty
    item["is_draft"] = False
    item["resolved"] = False

    garden_data.put_log(
        garden_id=garden_id,
        instance_id=instance_id,
        occurred_at=occurred_at,
        action_id=action_id,
        item=item,
    )

    nickname = plant.get("nickname", instance_id)

    logger.info(
        "Action log created: garden_id=%s instance_id=%s action_type=%s action_id=%s",
        garden_id, instance_id, action_type, action_id,
    )
    return f"Action log saved: {action_type} for '{nickname}' (action_id={action_id})."


@tool
def add_plant(
    species_id: str,
    nickname: str,
    emoji: str = "",
    garden_location: str = "",
) -> str:
    """Add a new plant to the current user's garden.

    Call get_plant_species first to confirm the species_id exists.

    Args:
        species_id: Lowercase hyphenated scientific name (e.g. 'rosa', 'solanum-lycopersicum').
        nickname: A friendly name for the plant (e.g. 'Big Red').
        emoji: Optional emoji for the plant (e.g. '🌹').
        garden_location: Optional location in the garden (e.g. 'South bed').
    """
    import uuid  # noqa: PLC0415

    # The target garden comes from the authorized context, never from the model.
    garden_id = _current_garden_id()
    if not garden_id:
        return NO_GARDEN_CONTEXT_ERROR

    # Same checks as POST /api/v1/plants.
    if not species_id or not nickname:
        return "Error: 'species_id' and 'nickname' are both required."

    species_data = species_dal.get_species(species_id)
    if not species_data:
        return (
            f"Error: Species '{species_id}' not found. Species IDs are lowercase "
            "hyphenated scientific names, e.g. 'rosa' or 'solanum-lycopersicum'."
        )

    now = datetime.now(timezone.utc).isoformat()
    instance_id = str(uuid.uuid4())

    item = {
        "species_id": species_id,
        "nickname": nickname,
        "emoji": emoji or None,
        "garden_location": garden_location or None,
        "planted_at": now,
        "status": "thriving",
        "updated_at": now,
    }
    garden_data.put_plant(garden_id, instance_id, item)

    logger.info(
        "Plant added by agent: garden_id=%s instance_id=%s species_id=%s",
        garden_id, instance_id, species_id,
    )
    common_name = species_data.get("common_name", species_id)
    return f"Plant added: '{nickname}' ({common_name}), instance_id={instance_id}."


def run_chat(garden_id: str, message: str) -> str:
    """Run a synchronous conversational agent turn for the given garden.

    Loads the existing conversation history from DynamoDB, runs the agent
    with the new message, then saves the updated history back. Returns the
    agent's plain-text reply.

    Args:
        garden_id: The garden to chat about.
        message: The user's message for this turn.
    """
    # Lock in the authorized garden before the agent runs. Every garden tool
    # reads it from the ContextVar, so the model cannot override it.
    set_authorized_garden(garden_id)

    try:
        metadata = garden_data.get_garden_metadata(garden_id)
        latitude = metadata.get("latitude") if metadata else None
        longitude = metadata.get("longitude") if metadata else None
        city = metadata.get("city", "the garden location") if metadata else "the garden"

        if latitude is not None and longitude is not None:
            weather_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={latitude}&longitude={longitude}"
                f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation"
                f"&forecast_days=3&timezone=auto"
            )
            location_context = (
                f"The garden is in {city} (lat={latitude}, lon={longitude}). "
                f"If the user asks about weather, fetch it via GET: {weather_url}"
            )
        else:
            location_context = "No location data is available for this garden."

        # Load saved conversation history (single session per garden — see garden_data.py)
        history = garden_data.get_chat_session(garden_id)

        agent = Agent(
            system_prompt=f"{CHAT_SYSTEM_PROMPT}\n\nGarden context: {location_context}",
            tools=[
                http_request,
                get_garden_plants,
                get_plant_species,
                get_recent_plant_logs,
                add_plant,
                create_action_log,
            ],
            model=BedrockModel(
                model_id="amazon.nova-pro-v1:0",
                region_name=BEDROCK_REGION,
            ),
            messages=history,
        )

        result = agent(message)
        reply = str(result).strip()

        # Strip <thinking>...</thinking> blocks that some models include in output
        import re  # noqa: PLC0415
        reply = re.sub(r"<thinking>.*?</thinking>", "", reply, flags=re.DOTALL).strip()

        if not reply:
            reply = "I wasn't able to generate a response. Please try again."

        # Persist the updated conversation history
        try:
            garden_data.put_chat_session(garden_id, agent.messages)
        except Exception as save_exc:  # noqa: BLE001
            error_str = str(save_exc).lower()
            if "item size" in error_str or "exceeded the maximum" in error_str:
                logger.warning(
                    "Chat session too large to save for garden_id=%s — history not persisted",
                    garden_id,
                )
                return (
                    reply
                    + "\n\n⚠️ Your conversation history has grown too long to save. "
                    "Please reset the conversation and start fresh:\n"
                    "  DELETE /api/v1/agent/chat"
                )
            logger.error(
                "Failed to save chat session for garden_id=%s: %s", garden_id, save_exc
            )

        return reply

    except Exception as exc:  # noqa: BLE001
        logger.exception("Chat failed for garden_id=%s: %s", garden_id, exc)
        raise
