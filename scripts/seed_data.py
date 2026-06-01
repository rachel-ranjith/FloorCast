#!/usr/bin/env python
"""Seed a realistic fake data centre into DynamoDB.

Generates (defaults, all overridable via flags):
    2 buildings x 4 suites x 48 rows x 10 positions
    ~12% of positions populated with a mix of 2023/2024 compute/storage/AI racks

All power draws and capacities are pulled from config/floorcast.yaml — nothing
here is hardcoded. Use --dry-run to emit JSON + a load report without touching AWS.

Examples:
    # Offline preview (no AWS), writes floorcast.seed.json
    python scripts/seed_data.py --dry-run

    # Seed DynamoDB Local
    DYNAMODB_ENDPOINT_URL=http://localhost:8000 python scripts/seed_data.py

    # Bigger floor
    python scripts/seed_data.py --positions-per-row 20 --fill-ratio 0.25
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal

from config.settings import get_settings
from floorcast.db.dynamo import tables as T
from floorcast.models.topology import Building
from floorcast.services.generator import GeneratedFloor, GeneratorConfig, generate_floor


# --------------------------------------------------------------------------- #
# Map domain objects -> DynamoDB items (single-table design).
# --------------------------------------------------------------------------- #
def _d(v: float) -> Decimal:
    return Decimal(str(v))


def floor_to_items(floor: GeneratedFloor, settings) -> list[dict]:
    items: list[dict] = []
    rack_by_id = {r.rack_id: r for r in floor.racks}

    for b in floor.buildings:
        items.append(
            {
                "PK": T.building_pk(b.building_id),
                "SK": "META",
                "entity": "building",
                "building_id": b.building_id,
                "label": b.label,
                "capacity_mw": _d(b.capacity_mw),
            }
        )
        for s in b.suites:
            items.append(
                {
                    "PK": T.building_pk(b.building_id),
                    "SK": T.suite_sk(s.suite_id),
                    "entity": "suite",
                    "suite_id": s.suite_id,
                    "building_id": b.building_id,
                    "label": s.label,
                    "ordinal": s.ordinal,
                    "capacity_mw": _d(s.capacity_mw),
                }
            )
            for row in s.rows:
                items.append(
                    {
                        "PK": T.suite_pk(s.suite_id),
                        "SK": T.row_sk(row.row_id),
                        "entity": "row",
                        "row_id": row.row_id,
                        "suite_id": s.suite_id,
                        "building_id": b.building_id,
                        "label": row.label,
                        "ordinal": row.ordinal,
                        "capacity_kw": _d(row.capacity_kw),
                    }
                )
                for pos in row.positions:
                    pos_item = {
                        "PK": T.row_pk(row.row_id),
                        "SK": T.position_sk(pos.ordinal),
                        "entity": "position",
                        "position_id": pos.position_id,
                        "row_id": row.row_id,
                        "suite_id": s.suite_id,
                        "building_id": b.building_id,
                        "ordinal": pos.ordinal,
                        "occupied": pos.occupied,
                    }
                    # The occupant pointer must NOT be stored under "rack_id":
                    # that name is the gsi-generation range key and may only
                    # appear on rack items (a NULL or stray value here either
                    # fails the write or wrongly couples positions to the index).
                    # Use a distinct attribute, and omit it entirely when empty.
                    if pos.rack_id is not None:
                        pos_item["occupant_rack_id"] = pos.rack_id
                    items.append(pos_item)

    # Rack entities (own partition). These are the ONLY items that carry the
    # gsi-generation / gsi-power key attributes — attached via the single helper.
    for rack in rack_by_id.values():
        rack_item = {
            "PK": T.rack_pk(rack.rack_id),
            "SK": "META",
            "entity": "rack",
            "position_id": rack.position_id,
            "row_id": rack.row_id,
            "suite_id": rack.suite_id,
            "building_id": rack.building_id,
            "rack_type": rack.rack_type,
            "family": rack.family,
            "state": rack.state.value,
            "installed_at": rack.installed_at.isoformat(),
            "updated_at": rack.updated_at.isoformat(),
        }
        # generation, rack_id, power_bucket, power_draw_kw (GSI keys) live here only.
        rack_item.update(
            T.rack_gsi_attributes(
                rack_id=rack.rack_id,
                generation=rack.generation,
                power_draw_kw=rack.power_draw_kw,
                building_id=rack.building_id,
            )
        )
        items.append(rack_item)
    return items


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def load_report(floor: GeneratedFloor, settings) -> dict:
    suite_cap_kw = settings.power.suite_capacity_mw * 1000.0
    bld_cap_kw = settings.power.building_capacity_mw * 1000.0
    row_cap_kw = settings.power.row_capacity_kw
    usable_pct = 1.0 - settings.power.headroom_pct
    row_usable_kw = row_cap_kw * usable_pct

    suite_load: dict[str, float] = {}
    bld_load: dict[str, float] = {}
    row_load: dict[str, float] = {}
    by_type: dict[str, int] = {}
    for r in floor.racks:
        suite_load[r.suite_id] = suite_load.get(r.suite_id, 0.0) + r.power_draw_kw
        bld_load[r.building_id] = bld_load.get(r.building_id, 0.0) + r.power_draw_kw
        row_load[r.row_id] = row_load.get(r.row_id, 0.0) + r.power_draw_kw
        by_type[r.rack_type] = by_type.get(r.rack_type, 0) + 1

    # Count every row in the topology (rows with no racks draw 0 kW and are
    # never over headroom, but they belong in the denominator).
    total_rows = sum(len(s.rows) for b in floor.buildings for s in b.suites)

    # Rows currently exceeding the row-level headroom threshold, hottest first.
    over_rows = [
        {
            "row_id": rid,
            "load_kw": round(load, 1),
            "usable_kw": round(row_usable_kw, 1),
            "util_pct": round(100 * load / row_cap_kw, 1),
        }
        for rid, load in row_load.items()
        if load > row_usable_kw
    ]
    over_rows.sort(key=lambda r: r["load_kw"], reverse=True)

    return {
        "total_racks": len(floor.racks),
        "racks_by_type": dict(sorted(by_type.items())),
        "building_load": {
            k: {"kw": round(v, 1), "util_pct": round(100 * v / bld_cap_kw, 1),
                "within_headroom": v <= bld_cap_kw * usable_pct}
            for k, v in sorted(bld_load.items())
        },
        "suite_load": {
            k: {"kw": round(v, 1), "util_pct": round(100 * v / suite_cap_kw, 1),
                "within_headroom": v <= suite_cap_kw * usable_pct}
            for k, v in sorted(suite_load.items())
        },
        "row_headroom": {
            "row_capacity_kw": row_cap_kw,
            "usable_threshold_kw": round(row_usable_kw, 1),
            "headroom_pct": settings.power.headroom_pct,
            "total_rows": total_rows,
            "rows_over_headroom": len(over_rows),
            "max_row_load_kw": round(max(row_load.values()), 1) if row_load else 0.0,
            "over_rows": over_rows,
        },
    }


# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--buildings", type=int, default=2)
    p.add_argument("--suites-per-building", type=int, default=4)
    p.add_argument("--rows-per-suite", type=int, default=48)
    p.add_argument("--positions-per-row", type=int, default=10)
    p.add_argument("--fill-ratio", type=float, default=0.12)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--dry-run", action="store_true",
                   help="Don't write to DynamoDB; dump items + report to floorcast.seed.json")
    p.add_argument("--out", default="floorcast.seed.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()

    gcfg = GeneratorConfig(
        buildings=args.buildings,
        suites_per_building=args.suites_per_building,
        rows_per_suite=args.rows_per_suite,
        positions_per_row=args.positions_per_row,
        fill_ratio=args.fill_ratio,
        seed=args.seed,
    )
    floor = generate_floor(settings, gcfg)
    items = floor_to_items(floor, settings)
    report = load_report(floor, settings)

    print("Generated floor:")
    print(f"  buildings={args.buildings} suites/bld={args.suites_per_building} "
          f"rows/suite={args.rows_per_suite} pos/row={args.positions_per_row}")
    print(f"  {report['total_racks']} racks, {len(items)} DynamoDB items")
    print("  racks by type:", report["racks_by_type"])
    for sid, info in report["suite_load"].items():
        flag = "OK " if info["within_headroom"] else "OVER"
        print(f"  [{flag}] suite {sid}: {info['kw']} kW ({info['util_pct']}%)")

    rh = report["row_headroom"]
    print(f"\nRow headroom (cap {rh['row_capacity_kw']} kW, "
          f"{int(rh['headroom_pct'] * 100)}% headroom -> usable {rh['usable_threshold_kw']} kW):")
    print(f"  {rh['total_rows']} rows total, {rh['rows_over_headroom']} over headroom, "
          f"max single-row load {rh['max_row_load_kw']} kW")
    for row in rh["over_rows"]:
        print(f"  [OVER] row {row['row_id']}: {row['load_kw']} kW "
              f"(usable {row['usable_kw']} kW, {row['util_pct']}% of cap)")

    if args.dry_run:
        payload = {
            "items": json.loads(json.dumps(items, default=str)),
            "report": report,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\n✓ dry-run: wrote {len(items)} items + report to {args.out}")
        return 0

    from floorcast.db.dynamo.client import get_table  # lazy: avoids boto3 for --dry-run

    table = get_table(settings)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    print(f"\n✓ wrote {len(items)} items to DynamoDB table '{settings.dynamo.table_name}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
