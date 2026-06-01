"""DynamoDB table definition for Floorcast live state.

Single-table design. One table holds the entire topology hierarchy plus the
live rack fleet. Access patterns drive the key design:

  Entity         PK                      SK
  -------------  ----------------------  --------------------------------
  Building       BUILDING#<bid>          META
  Suite          BUILDING#<bid>          SUITE#<sid>
  Row            SUITE#<sid>             ROW#<rid>
  RackPosition   ROW#<rid>               POS#<ordinal padded>
  Rack           RACK#<rack_id>          META

Access patterns:
  - Get a building's suites .......... Query PK=BUILDING#<bid>, SK begins_with SUITE#
  - Get a suite's rows ............... Query PK=SUITE#<sid>,    SK begins_with ROW#
  - Get a row's positions ............ Query PK=ROW#<rid>,      SK begins_with POS#
  - Get a rack by id ................. GetItem PK=RACK#<id>, SK=META
  - Heatmap: racks by power band ..... Query GSI gsi-power, PK=power_bucket
  - Fleet by generation (for opt) .... Query GSI gsi-generation, PK=generation

GSIs:
  gsi-power      : HASH power_bucket (e.g. "BLD#b1#0-10kw"), RANGE power_draw_kw
  gsi-generation : HASH generation  (e.g. "GEN#2023"),       RANGE rack_id

This module returns the create_table kwargs so it can be driven by boto3, the
CLI script, or moto in tests — no hardcoded table name (comes from settings).
"""

from __future__ import annotations

from typing import Any

from config.settings import Settings


def table_definition(settings: Settings) -> dict[str, Any]:
    """Return boto3 `create_table` kwargs for the single Floorcast table."""
    return {
        "TableName": settings.dynamo.table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "power_bucket", "AttributeType": "S"},
            {"AttributeName": "power_draw_kw", "AttributeType": "N"},
            {"AttributeName": "generation", "AttributeType": "S"},
            {"AttributeName": "rack_id", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": settings.dynamo.gsi_power,
                "KeySchema": [
                    {"AttributeName": "power_bucket", "KeyType": "HASH"},
                    {"AttributeName": "power_draw_kw", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": settings.dynamo.gsi_generation,
                "KeySchema": [
                    {"AttributeName": "generation", "KeyType": "HASH"},
                    {"AttributeName": "rack_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "Tags": [
            {"Key": "app", "Value": "floorcast"},
            {"Key": "env", "Value": settings.env},
        ],
    }


# --------------------------------------------------------------------------- #
# Key + attribute builders. Keep ALL key formatting here so reads and writes
# never drift apart.
# --------------------------------------------------------------------------- #
def building_pk(building_id: str) -> str:
    return f"BUILDING#{building_id}"


def suite_sk(suite_id: str) -> str:
    return f"SUITE#{suite_id}"


def suite_pk(suite_id: str) -> str:
    return f"SUITE#{suite_id}"


def row_sk(row_id: str) -> str:
    return f"ROW#{row_id}"


def row_pk(row_id: str) -> str:
    return f"ROW#{row_id}"


def position_sk(ordinal: int) -> str:
    return f"POS#{ordinal:04d}"


def rack_pk(rack_id: str) -> str:
    return f"RACK#{rack_id}"


def generation_gsi_pk(generation: int) -> str:
    return f"GEN#{generation}"


def power_bucket(building_id: str, power_draw_kw: float, band_kw: float = 10.0) -> str:
    """Bucket a rack into a power band for heatmap GSI queries.

    band_kw is the bucket width; lower bound is inclusive. Scoped per building
    so a heatmap query targets one building's partition.
    """
    lower = int(power_draw_kw // band_kw) * int(band_kw)
    upper = lower + int(band_kw)
    return f"BLD#{building_id}#{lower}-{upper}kw"


# --------------------------------------------------------------------------- #
# Sparse-index contract.
# --------------------------------------------------------------------------- #
# Both GSIs are SPARSE: an item shows up in an index only if it carries that
# index's key attributes, and any key attribute that IS present must match the
# declared scalar type — DynamoDB rejects NULL (and the wrong type) on a key.
#
#   gsi-generation : HASH generation (S),   RANGE rack_id (S)
#   gsi-power      : HASH power_bucket (S),  RANGE power_draw_kw (N)
#
# Therefore these four attributes belong ONLY on rack items. Never attach
# `rack_id`, `generation`, `power_bucket`, or `power_draw_kw` to a building,
# suite, row, or position item (a position's occupant goes under a different
# name, e.g. `occupant_rack_id`). Build rack items' index attributes via
# rack_gsi_attributes() so they live in exactly one place.
GSI_GENERATION_KEYS = ("generation", "rack_id")
GSI_POWER_KEYS = ("power_bucket", "power_draw_kw")


def rack_gsi_attributes(
    rack_id: str,
    generation: int,
    power_draw_kw: float,
    building_id: str,
    band_kw: float = 10.0,
) -> dict[str, Any]:
    """Return the GSI key attributes for a rack item (the only items indexed).

    `power_draw_kw` is emitted as a Decimal so it is a valid numeric GSI key.
    """
    from decimal import Decimal

    return {
        "generation": generation_gsi_pk(generation),
        "rack_id": rack_id,
        "power_bucket": power_bucket(building_id, float(power_draw_kw), band_kw),
        "power_draw_kw": Decimal(str(power_draw_kw)),
    }
