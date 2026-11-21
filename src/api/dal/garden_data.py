"""Data access layer for the single GardenApp DynamoDB table — garden-scoped entities.

Key scheme:
  PK = GARDEN#<garden_id>
  SK = PLANT#<instance_id>
     | LOG#<instance_id>#<occurred_at>#<action_id>
     | PHOTO#<instance_id>#<taken_at>#<photo_id>
     | ANALYSIS#<photo_id>
     | WATERING#<scheduled_date>#<instance_id>  (see watering_schedule.py)
     | DASHBOARD
"""

import os
import logging
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger(__name__)

_table = None


def _get_table():
    global _table
    if _table is None:
        table_name = os.environ.get("TABLE_NAME", "GardenApp-dev")
        _table = boto3.resource("dynamodb").Table(table_name)
    return _table


# ---------------------------------------------------------------------------
# Garden Metadata
# ---------------------------------------------------------------------------

def get_garden_metadata(garden_id: str) -> Optional[dict]:
    """Get the garden metadata item (name, location, etc.)."""
    table = _get_table()
    resp = table.get_item(Key={"pk": f"GARDEN#{garden_id}", "sk": "METADATA"})
    return resp.get("Item")


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------

def list_plants(garden_id: str) -> list[dict]:
    """Query all non-removed plant instances for a garden. (Access Pattern #1)"""
    table = _get_table()
    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"GARDEN#{garden_id}") & Key("sk").begins_with("PLANT#")
        ),
        FilterExpression=Attr("status").ne("removed"),
    )
    return resp.get("Items", [])


def get_plant(garden_id: str, instance_id: str) -> Optional[dict]:
    """Get a single plant instance by garden_id + instance_id. (Access Pattern #2)"""
    table = _get_table()
    resp = table.get_item(
        Key={"pk": f"GARDEN#{garden_id}", "sk": f"PLANT#{instance_id}"}
    )
    return resp.get("Item")


def put_plant(garden_id: str, instance_id: str, item: dict) -> None:
    """Write a new plant instance to the table."""
    table = _get_table()
    item = dict(item)
    item["pk"] = f"GARDEN#{garden_id}"
    item["sk"] = f"PLANT#{instance_id}"
    item["entity_type"] = "plant"
    item["instance_id"] = instance_id
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Action Logs
# ---------------------------------------------------------------------------

def list_logs(garden_id: str, sk_prefix: str = "LOG#") -> list[dict]:
    """Query all action logs for a garden, sorted descending. (Access Pattern #6)"""
    table = _get_table()
    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"GARDEN#{garden_id}") & Key("sk").begins_with(sk_prefix)
        ),
        ScanIndexForward=False,
    )
    return resp.get("Items", [])


def list_logs_for_plant(garden_id: str, instance_id: str) -> list[dict]:
    """Query all action logs for a specific plant instance, sorted descending."""
    return list_logs(garden_id, sk_prefix=f"LOG#{instance_id}#")


def list_logs_paginated(
    garden_id: str, instance_id: str, limit: int = 20, offset: int = 0
) -> dict:
    """Query paginated action logs for a plant instance. (Access Pattern #4)

    Returns {"items": [...], "total": N}.
    Note: offset pagination is implemented by fetching offset+limit items and
    slicing. Suitable at prototype scale; cursor-based pagination can replace
    this without changing the API contract.
    """
    table = _get_table()
    sk_prefix = f"LOG#{instance_id}#"

    # Count query
    count_resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"GARDEN#{garden_id}") & Key("sk").begins_with(sk_prefix)
        ),
        Select="COUNT",
    )
    total = count_resp.get("Count", 0)

    if total == 0 or offset >= total:
        return {"items": [], "total": total}

    # Page query — fetch offset+limit then slice
    fetch_limit = offset + limit
    page_resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"GARDEN#{garden_id}") & Key("sk").begins_with(sk_prefix)
        ),
        ScanIndexForward=False,
        Limit=fetch_limit,
    )
    items = page_resp.get("Items", [])[offset:]
    return {"items": items, "total": total}


def put_log(
    garden_id: str,
    instance_id: str,
    occurred_at: str,
    action_id: str,
    item: dict,
) -> None:
    """Write an action log item to the table. (Access Pattern #8)"""
    table = _get_table()
    item = dict(item)
    item["pk"] = f"GARDEN#{garden_id}"
    item["sk"] = f"LOG#{instance_id}#{occurred_at}#{action_id}"
    item["entity_type"] = "log"
    item["instance_id"] = instance_id
    item["action_id"] = action_id
    item["occurred_at"] = occurred_at
    table.put_item(Item=item)


def update_log_fields(garden_id: str, sk: str, fields: dict) -> None:
    """Update specific fields on an existing action log item using UpdateItem."""
    if not fields:
        return
    table = _get_table()
    update_expr = "SET " + ", ".join(f"#f_{k} = :v_{k}" for k in fields)
    expr_names = {f"#f_{k}": k for k in fields}
    expr_vals = {f":v_{k}": v for k, v in fields.items()}
    table.update_item(
        Key={"pk": f"GARDEN#{garden_id}", "sk": sk},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_vals,
    )


# ---------------------------------------------------------------------------
# Chat Session
# ---------------------------------------------------------------------------
# Design note: a single chat session per garden is used (SK=CHAT_SESSION).
# This keeps the demo simple — there is no multi-session or per-user history.
# A production system would key sessions by user and/or conversation ID.

def get_chat_session(garden_id: str) -> list:
    """Return the saved conversation messages for the garden's chat session.

    Returns an empty list when no session exists yet.
    """
    table = _get_table()
    resp = table.get_item(Key={"pk": f"GARDEN#{garden_id}", "sk": "CHAT_SESSION"})
    item = resp.get("Item")
    return item.get("messages", []) if item else []


def put_chat_session(garden_id: str, messages: list) -> None:
    """Persist the full conversation message list for the garden's chat session."""
    from datetime import datetime, timezone  # noqa: PLC0415
    table = _get_table()
    table.put_item(Item={
        "pk": f"GARDEN#{garden_id}",
        "sk": "CHAT_SESSION",
        "entity_type": "chat_session",
        "messages": messages,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def delete_chat_session(garden_id: str) -> None:
    """Delete the chat session history for a garden (reset conversation)."""
    table = _get_table()
    table.delete_item(Key={"pk": f"GARDEN#{garden_id}", "sk": "CHAT_SESSION"})


# ---------------------------------------------------------------------------
# Dashboard Summary
# ---------------------------------------------------------------------------

def get_dashboard_summary(garden_id: str) -> Optional[dict]:
    """Get the cached dashboard summary for a garden. (Access Pattern #3 partial)"""
    table = _get_table()
    resp = table.get_item(Key={"pk": f"GARDEN#{garden_id}", "sk": "DASHBOARD"})
    return resp.get("Item")


def put_dashboard_summary(garden_id: str, item: dict) -> None:
    """Write or overwrite the dashboard summary for a garden."""
    table = _get_table()
    item = dict(item)
    item["pk"] = f"GARDEN#{garden_id}"
    item["sk"] = "DASHBOARD"
    item["entity_type"] = "dashboard"
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

def list_photos(garden_id: str, instance_id: str) -> list[dict]:
    """Query all photos for a plant instance, sorted descending by taken_at. (Access Pattern #11)"""
    table = _get_table()
    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"GARDEN#{garden_id}")
            & Key("sk").begins_with(f"PHOTO#{instance_id}#")
        ),
        ScanIndexForward=False,
    )
    return resp.get("Items", [])


def get_photo_by_id(garden_id: str, photo_id: str) -> Optional[dict]:
    """Find a photo item by photo_id within a garden.

    Because the SK encodes taken_at (unknown at query time), we query all
    photos and filter in-app. At prototype scale (<100 photos per plant) this
    is fine. A GSI on photo_id would remove the need for a Query+filter.
    """
    table = _get_table()
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"GARDEN#{garden_id}"),
        FilterExpression=Attr("photo_id").eq(photo_id) & Attr("entity_type").eq("photo"),
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def put_photo(garden_id: str, instance_id: str, taken_at: str, photo_id: str, item: dict) -> None:
    """Write a photo metadata item to the table. (Access Pattern #23)"""
    table = _get_table()
    item = dict(item)
    item["pk"] = f"GARDEN#{garden_id}"
    item["sk"] = f"PHOTO#{instance_id}#{taken_at}#{photo_id}"
    item["entity_type"] = "photo"
    item["instance_id"] = instance_id
    item["photo_id"] = photo_id
    item["taken_at"] = taken_at
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# AI Analysis
# ---------------------------------------------------------------------------

def get_analysis(garden_id: str, photo_id: str) -> Optional[dict]:
    """Get the AI analysis for a photo. (Access Pattern #13)"""
    table = _get_table()
    resp = table.get_item(
        Key={"pk": f"GARDEN#{garden_id}", "sk": f"ANALYSIS#{photo_id}"}
    )
    return resp.get("Item")


def put_analysis(garden_id: str, photo_id: str, item: dict) -> None:
    """Write an AI analysis item. (Access Pattern #24)"""
    table = _get_table()
    item = dict(item)
    item["pk"] = f"GARDEN#{garden_id}"
    item["sk"] = f"ANALYSIS#{photo_id}"
    item["entity_type"] = "analysis"
    item["photo_id"] = photo_id
    table.put_item(Item=item)
