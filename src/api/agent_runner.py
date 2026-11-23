"""Strands agent runner for the garden session-open async task.

The agent reads the current garden state, fetches local weather, and writes
a refreshed dashboard summary back to DynamoDB.
"""

import json
import logging
import os
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from src.api.dal import garden_data, species as species_dal
from src.api.enums import ActionType, LoggedBy, Severity
from src.api.errors import ApiError
from src.api.services import garden_service

# ---------------------------------------------------------------------------
# Model-facing value lists
# ---------------------------------------------------------------------------
# Generated from the same enums garden_service validates against, so the
# prompt and tool descriptions can't drift from what the service accepts.
ACTION_TYPES_TEXT = ", ".join(a.value for a in ActionType)
SEVERITIES_TEXT = ", ".join(s.value for s in Severity)


def _format_doc(**values):
    """Fill {placeholders} in a tool docstring before @tool reads it."""
    def decorate(fn):
        fn.__doc__ = fn.__doc__.format(**values)
        return fn
    return decorate


def _tool_error(exc: ApiError) -> str:
    """Turn a service error into a tool result the model can act on."""
    message = f"Error: {exc.message}"
    if exc.status_code == 404:
        message += " Use get_garden_plants to find valid plant IDs. To add a new plant, use add_plant."
    return message

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

# The runners bind the garden with the token pattern:
#     token = _authorized_garden_id.set(garden_id)
#     try: ... run the agent ...
#     finally: _authorized_garden_id.reset(token)
# The reset matters on Lambda. A warm container handles the next invocation in
# the same context, so a value that is only ever set would still be bound when
# the next request starts.


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
4. Call get_weather to get current conditions and a 3-day forecast for the
   garden. If it returns an error, continue without weather.
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


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
# A narrow tool in place of a general HTTP tool. The URL is fixed in code and
# the coordinates come from the garden's stored metadata, so the model can only
# decide *when* to ask for weather, never where a request goes or what it sends.

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT_SECONDS = 10
WEATHER_UNAVAILABLE_ERROR = "Error: The weather service is unavailable right now."


def _fetch_json(url: str) -> dict:
    """GET a URL and parse the JSON body. Kept separate so tests can replace it."""
    with urllib.request.urlopen(url, timeout=WEATHER_TIMEOUT_SECONDS) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read())


@tool
def get_weather() -> dict:
    """Get current weather and a 3-day forecast for the current user's garden.

    The garden's location is looked up automatically. Returns current
    temperature, humidity, wind speed and precipitation, plus daily max/min
    temperature, total precipitation and max wind speed for the next 3 days,
    with the units for each value.
    """
    garden_id = _current_garden_id()
    if not garden_id:
        return {"error": NO_GARDEN_CONTEXT_ERROR}

    metadata = garden_data.get_garden_metadata(garden_id) or {}
    latitude, longitude = metadata.get("latitude"), metadata.get("longitude")
    if latitude is None or longitude is None:
        return {"error": "Error: No location is stored for this garden, so weather is unavailable."}

    query = urllib.parse.urlencode({
        "latitude": float(latitude),
        "longitude": float(longitude),
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
        "forecast_days": 3,
        "timezone": "auto",
    })
    try:
        data = _fetch_json(f"{OPEN_METEO_URL}?{query}")
    except (OSError, ValueError) as exc:  # network errors, timeouts, bad JSON
        logger.warning("Weather fetch failed for garden_id=%s: %s", garden_id, exc)
        return {"error": WEATHER_UNAVAILABLE_ERROR}

    # Return only what the agent needs, not the raw API response.
    return {
        "location": metadata.get("city"),
        "current": data.get("current", {}),
        "current_units": data.get("current_units", {}),
        "daily": data.get("daily", {}),
        "daily_units": data.get("daily_units", {}),
    }


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
    # Scope every garden tool to this garden for this run only.
    token = _authorized_garden_id.set(garden_id)
    try:
        metadata = garden_data.get_garden_metadata(garden_id)
        if not metadata:
            logger.error("run_session_open: no garden metadata for garden_id=%s", garden_id)
            return

        city = metadata.get("city", "an unknown location")

        agent = Agent(
            system_prompt=SYSTEM_PROMPT,
            tools=[
                get_weather,
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
            f"Please assess the garden in {city} and generate a dashboard summary.\n\n"
            f"Work through the steps in your instructions, then call save_dashboard_summary."
        )

        logger.info("Starting agent session for garden_id=%s", garden_id)
        agent(prompt)
        logger.info("Agent session complete for garden_id=%s", garden_id)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent session failed for garden_id=%s: %s", garden_id, exc)
    finally:
        # Unbind, so a warm Lambda container doesn't carry this garden into the next invocation.
        _authorized_garden_id.reset(token)


# ---------------------------------------------------------------------------
# Chat agent
# ---------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = f"""You are a friendly, knowledgeable garden assistant. You help users
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
   {ACTION_TYPES_TEXT}
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

If the user asks about weather, call get_weather. It already knows the garden's
location, so you don't need to ask for one.
Be concise, warm, and practical. Always end your response with a plain-text message
to the user — never finish on a tool call or a thinking block alone. Do not wrap
your response in <thinking> tags — write your answer directly as plain text."""


@tool
@_format_doc(action_types=ACTION_TYPES_TEXT, severities=SEVERITIES_TEXT)
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
        action_type: One of: {action_types}.
        notes: Optional free-text description of what was done.
        title: Optional short title for the log entry.
        severity: Optional severity for issue_found entries. One of: {severities}.
    """
    # garden_id comes from the authorized context set by run_chat, not from
    # the LLM.  This prevents the model from writing to a different garden.
    garden_id = _current_garden_id()
    if not garden_id:
        return NO_GARDEN_CONTEXT_ERROR

    # Validation, item format and the write are shared with
    # POST /plants/{id}/action-logs. The service rejects plants outside
    # garden_id, which covers invented IDs and other users' plants.
    try:
        created = garden_service.create_action_log(
            garden_id,
            instance_id,
            action_type=action_type,
            title=title,
            notes=notes,
            severity=severity,
            logged_by=LoggedBy.agent.value,
        )
    except ApiError as exc:
        return _tool_error(exc)

    logger.info(
        "Action log created: garden_id=%s instance_id=%s action_type=%s action_id=%s",
        garden_id, instance_id, created.item["action_type"], created.action_id,
    )
    nickname = created.plant.get("nickname", instance_id)
    return (
        f"Action log saved: {created.item['action_type']} for '{nickname}' "
        f"(action_id={created.action_id})."
    )


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
    # The target garden comes from the authorized context, never from the model.
    garden_id = _current_garden_id()
    if not garden_id:
        return NO_GARDEN_CONTEXT_ERROR

    # Validation, item format and the write are shared with POST /plants.
    try:
        created = garden_service.create_plant(
            garden_id,
            species_id=species_id,
            nickname=nickname,
            emoji=emoji,
            garden_location=garden_location,
        )
    except ApiError as exc:
        return _tool_error(exc)

    logger.info(
        "Plant added by agent: garden_id=%s instance_id=%s species_id=%s",
        garden_id, created.instance_id, species_id,
    )
    common_name = created.species.get("common_name", species_id)
    return f"Plant added: '{nickname}' ({common_name}), instance_id={created.instance_id}."


def run_chat(garden_id: str, message: str) -> str:
    """Run a synchronous conversational agent turn for the given garden.

    Loads the existing conversation history from DynamoDB, runs the agent
    with the new message, then saves the updated history back. Returns the
    agent's plain-text reply.

    Args:
        garden_id: The garden to chat about.
        message: The user's message for this turn.
    """
    # Bind the authorized garden for this run only. Every garden tool reads it
    # from the ContextVar, so the model cannot override it.
    token = _authorized_garden_id.set(garden_id)
    try:
        metadata = garden_data.get_garden_metadata(garden_id) or {}
        city = metadata.get("city")
        location_context = (
            f"The garden is in {city}." if city else "The garden's city isn't recorded."
        )

        # Load saved conversation history (single session per garden — see garden_data.py)
        history = garden_data.get_chat_session(garden_id)

        agent = Agent(
            system_prompt=f"{CHAT_SYSTEM_PROMPT}\n\nGarden context: {location_context}",
            tools=[
                get_weather,
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
    finally:
        # Unbind, so a warm Lambda container doesn't carry this garden into the next invocation.
        _authorized_garden_id.reset(token)
