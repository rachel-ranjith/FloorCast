#!/usr/bin/env python
"""Create the Floorcast DynamoDB table (idempotent).

Usage:
    python scripts/create_dynamo_tables.py            # uses config + env
    DYNAMODB_ENDPOINT_URL=http://localhost:8000 python scripts/create_dynamo_tables.py
"""

from __future__ import annotations

import sys

from botocore.exceptions import ClientError

from config.settings import get_settings
from floorcast.db.dynamo.client import get_resource
from floorcast.db.dynamo.tables import table_definition


def main() -> int:
    settings = get_settings()
    resource = get_resource(settings)
    definition = table_definition(settings)
    name = definition["TableName"]

    existing = {t.name for t in resource.tables.all()}
    if name in existing:
        print(f"✓ table '{name}' already exists — nothing to do")
        return 0

    try:
        table = resource.create_table(**definition)
        table.wait_until_exists()
        print(f"✓ created table '{name}' with GSIs "
              f"{settings.dynamo.gsi_power}, {settings.dynamo.gsi_generation}")
    except ClientError as exc:
        print(f"✗ failed to create table '{name}': {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
