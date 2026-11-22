#!/usr/bin/env python3
"""Seed the GardenApp DynamoDB table with minimal demo data.

Usage:
    python scripts/seed_demo_data.py [--env demo] [--region eu-west-2]
                                     [--garden-id demo-garden-1] [--user-id demo-user-1]

--garden-id and --user-id must match the stack's DemoGardenId and DemoUserId
parameters (infrastructure/template.yaml), since those are the IDs the Lambda
serves. The defaults match the template defaults.
"""
import argparse
import boto3
from datetime import datetime, timezone
from decimal import Decimal

parser = argparse.ArgumentParser()
parser.add_argument("--env", default="demo")
parser.add_argument("--region", default="eu-west-2")
parser.add_argument("--garden-id", default="demo-garden-1")
parser.add_argument("--user-id", default="demo-user-1")
args = parser.parse_args()

dynamodb = boto3.resource("dynamodb", region_name=args.region)

GARDEN_ID = args.garden_id
USER_ID = args.user_id
NOW = datetime.now(timezone.utc).isoformat()

table = dynamodb.Table(f"GardenApp-{args.env}")

# ── User profile ─────────────────────────────────────────────────────────────
table.put_item(Item={
    "pk": f"USER#{USER_ID}",
    "sk": "PROFILE",
    "entity_type": "user",
    "user_id": USER_ID,
    "garden_id": GARDEN_ID,
    "display_name": "Demo Gardener",
    "email": "demo@example.com",
    "created_at": NOW,
})
print(f"User profile: inserted {USER_ID}")

# ── Garden metadata ───────────────────────────────────────────────────────────
table.put_item(Item={
    "pk": f"GARDEN#{GARDEN_ID}",
    "sk": "METADATA",
    "entity_type": "garden",
    "garden_id": GARDEN_ID,
    "name": "Demo Garden",
    "latitude": Decimal("51.5074"),
    "longitude": Decimal("-0.1278"),
    "city": "London",
    "created_at": NOW,
})
print(f"Garden metadata: inserted {GARDEN_ID}")

# ── Plant instances ───────────────────────────────────────────────────────────
plants = [
    {
        "pk": f"GARDEN#{GARDEN_ID}",
        "sk": "PLANT#tomato-01",
        "entity_type": "plant",
        "instance_id": "tomato-01",
        "species_id": "solanum-lycopersicum",
        "nickname": "Big Red",
        "emoji": "🍅",
        "garden_location": "South bed",
        "planted_at": "2026-04-01T09:00:00+00:00",
        "status": "thriving",
        "next_watering_date": "2026-11-19T08:00:00+00:00",
        "updated_at": NOW,
    },
    {
        "pk": f"GARDEN#{GARDEN_ID}",
        "sk": "PLANT#basil-01",
        "entity_type": "plant",
        "instance_id": "basil-01",
        "species_id": "ocimum-basilicum",
        "nickname": "Herb Corner",
        "emoji": "🌿",
        "garden_location": "Kitchen windowsill",
        "planted_at": "2026-06-15T09:00:00+00:00",
        "status": "needs_attention",
        "next_watering_date": "2026-11-18T08:00:00+00:00",
        "updated_at": NOW,
    },
]

for plant in plants:
    table.put_item(Item=plant)
    print(f"Plant: inserted {plant['instance_id']}")

# ── Sample action log ─────────────────────────────────────────────────────────
table.put_item(Item={
    "pk": f"GARDEN#{GARDEN_ID}",
    "sk": "LOG#tomato-01#2026-11-17T10:00:00+00:00#log-001",
    "entity_type": "log",
    "action_id": "log-001",
    "instance_id": "tomato-01",
    "action_type": "watered",
    "logged_by": "user",
    "occurred_at": "2026-11-17T10:00:00+00:00",
    "notes": "Watered thoroughly after dry spell.",
    "is_draft": False,
    "image_refs": [],
})
print("Action log: inserted sample watering log for tomato-01")

print("\nSeed complete. Demo garden_id:", GARDEN_ID)
