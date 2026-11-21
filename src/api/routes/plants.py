"""Plant route handlers.

  GET  /api/v1/plants               — list all plants
  POST /api/v1/plants               — add a new plant
  GET  /api/v1/plants/{instance_id} — get a single plant
"""

import uuid
from datetime import datetime, timezone

from aws_lambda_powertools.event_handler.api_gateway import Router

from src.api.dal import garden_data, species
from src.api.errors import ApiError
from src.api.middleware import require_garden

router = Router()

_STATUS_LABELS = {
    "thriving": "Thriving",
    "needs_attention": "Needs attention",
    "alert": "Alert",
}


def _status_label(status: str | None) -> str | None:
    if status is None:
        return None
    return _STATUS_LABELS.get(status, status.replace("_", " ").title())


def _build_plant_response(plant: dict, species_data: dict | None) -> dict:
    """Merge plant item and species data into the canonical response shape."""
    sk = plant.get("sk", "")
    instance_id = sk.removeprefix("PLANT#") if sk.startswith("PLANT#") else plant.get("instance_id")

    species_common_name = None
    species_latin_name = None
    sun_requirement = None
    if species_data:
        species_common_name = species_data.get("common_name")
        species_latin_name = species_data.get("scientific_name")
        sun_requirement = species_data.get("sunlight_requirement")

    status = plant.get("status")

    return {
        "instance_id": instance_id,
        "species_id": plant.get("species_id"),
        "nickname": plant.get("nickname"),
        "emoji": plant.get("emoji"),
        "species_common_name": species_common_name,
        "species_latin_name": species_latin_name,
        "garden_location": plant.get("garden_location"),
        "planted_at": plant.get("planted_at"),
        "status": status,
        "status_label": _status_label(status),
        "next_watering_date": plant.get("next_watering_date"),
        "sun_requirement": sun_requirement,
        "updated_at": plant.get("updated_at"),
    }


@router.get("/plants")
@require_garden
def list_plants(garden_id: str, user_id: str) -> dict:
    """GET /api/v1/plants — list all non-removed plants with species join."""
    plants = garden_data.list_plants(garden_id)

    result = []
    for plant in plants:
        species_id = plant.get("species_id")
        species_data = species.get_species(species_id) if species_id else None
        result.append(_build_plant_response(plant, species_data))

    return {"plants": result}


@router.post("/plants")
@require_garden
def create_plant(garden_id: str, user_id: str) -> tuple:
    """POST /api/v1/plants — add a new plant to the garden.

    Required body fields: species_id, nickname
    Optional body fields: emoji, garden_location, planted_at
    """
    from src.api.router import app  # noqa: PLC0415

    body = app.current_event.json_body or {}

    species_id = body.get("species_id")
    nickname = body.get("nickname")

    missing = [f for f, v in [("species_id", species_id), ("nickname", nickname)] if not v]
    if missing:
        raise ApiError(400, "missing_field", f"Required field(s) missing: {missing}.")

    # Validate species exists
    species_data = species.get_species(species_id)
    if not species_data:
        raise ApiError(400, "not_found", f"Species '{species_id}' not found.")

    now = datetime.now(timezone.utc).isoformat()
    instance_id = str(uuid.uuid4())

    item = {
        "species_id": species_id,
        "nickname": nickname,
        "emoji": body.get("emoji"),
        "garden_location": body.get("garden_location"),
        "planted_at": body.get("planted_at") or now,
        "status": "thriving",
        "updated_at": now,
    }

    garden_data.put_plant(garden_id, instance_id, item)

    import json  # noqa: PLC0415
    from aws_lambda_powertools.event_handler.api_gateway import Response  # noqa: PLC0415

    response_body = _build_plant_response(
        {**item, "sk": f"PLANT#{instance_id}"},
        species_data,
    )
    return Response(
        status_code=201,
        content_type="application/json",
        body=json.dumps(response_body),
    )


@router.get("/plants/<instance_id>")
@require_garden
def get_plant(instance_id: str, garden_id: str, user_id: str) -> dict:
    """GET /api/v1/plants/{instance_id} — single plant detail."""
    plant = garden_data.get_plant(garden_id, instance_id)
    if not plant:
        raise ApiError(404, "not_found", f"Plant '{instance_id}' not found in this garden.")

    species_id = plant.get("species_id")
    species_data = species.get_species(species_id) if species_id else None

    response = _build_plant_response(plant, species_data)
    response["ai_insight_text"] = plant.get("ai_insight_text")
    return response
