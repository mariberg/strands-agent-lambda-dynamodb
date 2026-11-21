"""Data access layer for species in the single GardenApp DynamoDB table.

Key scheme:
  PK = SPECIES#<species_id>
  SK = METADATA
"""

import os
import logging
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

_table = None


def _get_table():
    global _table
    if _table is None:
        table_name = os.environ.get("TABLE_NAME", "GardenApp-dev")
        _table = boto3.resource("dynamodb").Table(table_name)
    return _table


def get_species(species_id: str) -> Optional[dict]:
    """Retrieve species metadata by species_id (e.g. 'solanum-lycopersicum').

    Returns the species dict with fields including common_name, scientific_name,
    sunlight_requirement, watering_frequency_days, etc. Returns None if not found.
    """
    table = _get_table()
    resp = table.get_item(Key={"pk": f"SPECIES#{species_id}", "sk": "METADATA"})
    return resp.get("Item")
