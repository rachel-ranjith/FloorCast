#!/usr/bin/env python
"""Seed the standard FloorCast data centre into DynamoDB.

Generates the engineered "tension" floor (generate_demo_floor): a heterogeneous
3-building estate (~254 racks) with binding throughput, swap-avoidable overage,
and three diverging targets per family, so the optimizer's objectives genuinely
trade off. Seeding is a full REPLACE by default (--no-purge to append).

All power draws and capacities are pulled from config/floorcast.yaml — nothing
here is hardcoded. --dry-run emits JSON + the tension report without touching AWS.

Examples:
    # Offline preview (no AWS), prints the tension report, writes floorcast.seed.json
    python scripts/seed_data.py --dry-run

    # Purge + seed DynamoDB Local
    DYNAMODB_ENDPOINT_URL=http://localhost:8000 python scripts/seed_data.py
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal

from config.settings import get_settings
from floorcast.db.dynamo import tables as T
from floorcast.models.topology import Building
from floorcast.services.generator import GeneratedFloor, generate_demo_floor


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
# Purge (seeding is a full replace, not an append)
# --------------------------------------------------------------------------- #
def purge_table(table) -> int:
    """Delete every item in the table (single-table design uses PK/SK keys).

    Seeding APPENDS by default in DynamoDB; without this, re-seeding a different
    floor leaves the previous floor's items behind (the Stage-6 contamination:
    default + demo floors coexisting). Fine to scan-all at seed scale.
    """
    keys: list[tuple] = []
    kwargs: dict = {}
    last = None
    while True:
        if last:
            kwargs["ExclusiveStartKey"] = last
        resp = table.scan(**kwargs)
        keys.extend((it["PK"], it["SK"]) for it in resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
    with table.batch_writer() as batch:
        for pk, sk in keys:
            batch.delete_item(Key={"PK": pk, "SK": sk})
    return len(keys)


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
# Demo-floor tension verification (report only — proves the floor forces
# objectives to diverge; does NOT run the optimizer).
# --------------------------------------------------------------------------- #
def _targets_by_family(settings) -> dict[str, list[tuple[str, object]]]:
    fam: dict[str, list[tuple[str, object]]] = {}
    for name, spec in settings.replacement_targets().items():
        fam.setdefault(spec.family, []).append((name, spec))
    return fam


def classify_rows(floor: GeneratedFloor, settings) -> dict[str, list[dict]]:
    """Bucket every occupied row into structural / swap-avoidable / cool overage.

    structural      : base load already > usable with ZERO swaps (immovable).
    swap-avoidable  : OK now, but some candidate's *best* (lowest-power) target
                      still tips the row over -> the violation is created by the
                      swap and avoided by skipping it.
    cool            : no swap can push it over (safe).
    """
    usable = settings.power.row_capacity_kw * (1.0 - settings.power.headroom_pct)
    fam_targets = _targets_by_family(settings)
    min_target_power = {
        f: min(spec.power_draw_kw for _n, spec in ts) for f, ts in fam_targets.items()
    }

    by_row: dict[str, list] = {}
    for r in floor.racks:
        by_row.setdefault(r.row_id, []).append(r)

    buckets: dict[str, list[dict]] = {"structural": [], "swap_avoidable": [], "cool": []}
    for rid, racks in by_row.items():
        base = sum(r.power_draw_kw for r in racks)
        cands = [r for r in racks if settings.is_retirement_candidate(r.rack_type)]
        info = {"row": rid, "base_kw": round(base, 1), "candidates": len(cands)}
        if base > usable + 1e-9:
            buckets["structural"].append({**info, "over_kw": round(base - usable, 1)})
            continue
        worst = 0.0
        for c in cands:
            mtp = min_target_power.get(c.family)
            if mtp is None:
                continue
            after = base - c.power_draw_kw + mtp  # swap THIS candidate to its lowest-power target
            worst = max(worst, after - usable)
        if worst > 1e-9:
            buckets["swap_avoidable"].append({**info, "min_induced_over_kw": round(worst, 1)})
        else:
            buckets["cool"].append(info)
    for b in buckets.values():
        b.sort(key=lambda d: d["row"])
    return buckets


def print_demo_report(floor: GeneratedFloor, settings) -> None:
    opt = settings.optimizer
    usable = settings.power.row_capacity_kw * (1.0 - settings.power.headroom_pct)
    cands = [r for r in floor.racks if settings.is_retirement_candidate(r.rack_type)]
    slots = opt.max_swaps_per_month * opt.horizon_months

    print("=" * 78)
    print("DEMO FLOOR — tension verification")
    print("=" * 78)

    # ---- topology ----
    print("\nTopology (heterogeneous estate):")
    total_rows = 0
    for b in floor.buildings:
        print(f"  {b.building_id} {b.label}: {len(b.suites)} suites")
        for s in b.suites:
            occ = sum(1 for row in s.rows for p in row.positions if p.occupied)
            total_rows += len(s.rows)
            print(f"      {s.suite_id} ({s.label}): {len(s.rows)} rows, {occ} racks")
    print(f"  totals: {len(floor.buildings)} buildings, "
          f"{sum(len(b.suites) for b in floor.buildings)} suites, {total_rows} rows, "
          f"{len(floor.racks)} racks")

    # ---- 1. throughput binds ----
    by_gen: dict[int, int] = {}
    by_fam: dict[str, int] = {}
    for r in cands:
        by_gen[r.generation] = by_gen.get(r.generation, 0) + 1
        by_fam[r.family] = by_fam.get(r.family, 0) + 1
    print("\n[1] THROUGHPUT BINDING")
    print(f"  candidates (retirable, gen<= {opt.retire_generation_at_or_below}): {len(cands)}"
          f"   by gen={dict(sorted(by_gen.items()))}  by family={dict(sorted(by_fam.items()))}")
    print(f"  swap slots over horizon: {opt.max_swaps_per_month}/mo x {opt.horizon_months}mo "
          f"= {slots}")
    binds = len(cands) > slots
    print(f"  binds? {binds}  -> only {slots}/{len(cands)} "
          f"({100*slots/max(1,len(cands)):.0f}%) of candidates can be done; the solver MUST "
          f"prioritize, so value-aware objectives pick different swaps.")

    # ---- 2. swap-avoidable vs structural overage ----
    buckets = classify_rows(floor, settings)
    safe_cands = sum(d["candidates"] for d in buckets["cool"])
    print("\n[2] OVERAGE TENSION (row usable = "
          f"{usable:.0f} kW @ {int(settings.power.headroom_pct*100)}% headroom)")
    print(f"  cool rows (no swap can overflow)        : {len(buckets['cool']):>3}  "
          f"-> {safe_cands} 'safe' candidates (zero-overage swaps)")
    print(f"  swap-AVOIDABLE overage rows             : {len(buckets['swap_avoidable']):>3}  "
          f"-> swapping their candidate creates a violation a smaller target/skip avoids")
    print(f"  structural (unavoidable) overage rows   : {len(buckets['structural']):>3}  "
          f"-> over with ZERO swaps; identical under every objective")
    print(f"  safe candidates ({safe_cands}) < slots ({slots}) < total candidates "
          f"({len(cands)}):  {safe_cands < slots < len(cands)}")
    print("    => maximize_racks_modernized must fill its quota with avoidable-overage swaps,")
    print("       while minimize_headroom_violation refuses them -> different swap count & overage.")
    print("  sample swap-avoidable rows:")
    for d in buckets["swap_avoidable"][:4]:
        print(f"      {d['row']}: base {d['base_kw']} kW (<= {usable:.0f}); cheapest swap still "
              f"+{d['min_induced_over_kw']} kW over")
    print("  sample structural rows:")
    for d in buckets["structural"][:3]:
        print(f"      {d['row']}: base {d['base_kw']} kW -> {d['over_kw']} kW over (immovable)")

    # ---- 3 & 4. cost / compute / efficiency divergence among targets ----
    print("\n[3/4] TARGET DIVERGENCE (cost vs compute vs efficiency)")
    for family, ts in sorted(_targets_by_family(settings).items()):
        rows = []
        for name, spec in ts:
            rows.append({
                "name": name, "kw": spec.power_draw_kw, "cost": spec.cost,
                "compute": spec.compute_capability,
                "per_pound": spec.compute_capability / spec.cost,
                "per_kw": spec.compute_capability / spec.power_draw_kw,
            })
        max_compute = max(rows, key=lambda d: d["compute"])["name"]
        best_value = max(rows, key=lambda d: d["per_pound"])["name"]
        best_eff = max(rows, key=lambda d: d["per_kw"])["name"]
        cheapest = min(rows, key=lambda d: d["cost"])["name"]
        print(f"  family '{family}': {len(rows)} targets")
        print(f"    {'target':<22}{'kW':>6}{'cost£':>9}{'compute':>9}"
              f"{'comp/£':>10}{'comp/kW':>9}")
        for d in rows:
            print(f"    {d['name']:<22}{d['kw']:>6.1f}{d['cost']:>9.0f}{d['compute']:>9.0f}"
                  f"{d['per_pound']:>10.5f}{d['per_kw']:>9.2f}")
        print(f"    -> maximize_compute={max_compute}  maximize_value_per_pound={best_value}  "
              f"maximize_efficiency={best_eff}")
        print(f"       cheapest={cheapest} differs from max-compute={max_compute}: "
              f"{cheapest != max_compute}")
    print("\n(Report only — no optimizer was run. Run the objectives yourself to see the plans diverge.)")


# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--positions-per-row", type=int, default=10)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--purge", action=argparse.BooleanOptionalAction, default=True,
                   help="Delete all existing table items before writing so the seed is a "
                        "clean full replace (default: on). Use --no-purge to append. "
                        "Ignored for --dry-run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't write to DynamoDB; dump items + tension report to floorcast.seed.json")
    p.add_argument("--out", default="floorcast.seed.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()

    floor = generate_demo_floor(settings, positions_per_row=args.positions_per_row, seed=args.seed)
    items = floor_to_items(floor, settings)
    print_demo_report(floor, settings)
    print()
    report = load_report(floor, settings)

    if args.dry_run:
        payload = {"items": json.loads(json.dumps(items, default=str)), "report": report}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"✓ dry-run: wrote {len(items)} items + report to {args.out}")
        return 0

    from floorcast.db.dynamo.client import get_table  # lazy: avoids boto3 for --dry-run

    table = get_table(settings)
    if args.purge:
        removed = purge_table(table)
        print(f"✓ purged {removed} pre-existing items from '{settings.dynamo.table_name}'")
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    print(f"✓ wrote {len(items)} items to DynamoDB table '{settings.dynamo.table_name}' "
          f"({len(floor.racks)} racks). Table now holds exactly these items.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
