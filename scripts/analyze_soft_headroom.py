#!/usr/bin/env python
"""THROWAWAY analysis of soft row-tier headroom. Does not touch the server or
the engine; it reconstructs the seeded floor and instruments direct solves.

Reconstructs the exact live-seeded fleet (generator seed=42, default config) and:
  1. Validates it reproduces the reported overage (42 @ 0.20, 468 @ 0.45).
  2. Breaks the 0.45 overage down by row, splitting each hot row's load into
     swappable (gen-2023 candidate) vs non-removable kW, and compares the solved
     overage to the structural minimum (sum of max(0, base_load - usable)).
  3. Compares penalty=1000 vs penalty=0 on the same fleet (placement + overage),
     and runs a tiny deterministic micro-fleet that isolates the placement effect.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config.settings import get_settings
from floorcast.models.rack import Rack, RackState
from floorcast.optimizer.engine import FleetInput, RackReplacementOptimizer, TierCapacities
from floorcast.services.generator import GeneratorConfig, generate_floor
from floorcast.services.optimization_service import resolve_settings

NOW = datetime.now(timezone.utc)


def build_seed_fleet(settings) -> FleetInput:
    """Same inputs the seed script used: defaults + seed=42."""
    floor = generate_floor(settings, GeneratorConfig(seed=42))
    caps = TierCapacities()
    for b in floor.buildings:
        caps.building_kw[b.building_id] = b.capacity_mw * 1000.0
        for s in b.suites:
            caps.suite_kw[s.suite_id] = s.capacity_mw * 1000.0
            for row in s.rows:
                caps.row_kw[row.row_id] = row.capacity_kw
    return FleetInput(racks=floor.racks, capacities=caps)


def solve(settings, fleet, headroom, penalty, tlimit=120):
    eff = resolve_settings(
        settings,
        {
            "power": {"headroom_pct": headroom},
            "optimizer": {"row_overage_penalty": penalty, "solver_time_limit_seconds": tlimit},
        },
    )
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="x", created_at=NOW)
    return eff, res


def row_base_loads(fleet: FleetInput) -> dict[str, float]:
    base: dict[str, float] = {}
    for r in fleet.racks:
        base[r.row_id] = base.get(r.row_id, 0.0) + r.power_draw_kw
    return base


def structural_overage(fleet, eff) -> tuple[float, list[dict]]:
    """Zero-swap minimum overage: swaps only raise load, so a row can never go
    below its base load -> max(0, base - usable) is immovable."""
    usable = eff.power.usable(eff.power.row_capacity_kw)
    base = row_base_loads(fleet)
    rows = []
    total = 0.0
    for rid, load in base.items():
        over = max(0.0, load - usable)
        if over > 1e-9:
            cand = sum(
                r.power_draw_kw for r in fleet.racks
                if r.row_id == rid and eff.is_retirement_candidate(r.rack_type)
            )
            noncand = load - cand
            rows.append({"row": rid, "base": load, "over": over,
                         "cand_kw": cand, "noncand_kw": noncand})
            total += over
    rows.sort(key=lambda d: d["over"], reverse=True)
    return total, rows


def swap_set(res):
    return {(s.position_id, s.month) for p in res.plan for s in p.swaps}


def overage_profile(res):
    return [round(p.row_overage_kw, 2) for p in res.plan]


def main():
    settings = get_settings()
    fleet = build_seed_fleet(settings)
    usable_045 = settings.power.row_capacity_kw * (1 - 0.45)

    print("=" * 78)
    print(f"Reconstructed fleet: {len(fleet.racks)} racks, {len(row_base_loads(fleet))} occupied rows")
    cand_total = sum(1 for r in fleet.racks if settings.is_retirement_candidate(r.rack_type))
    print(f"gen-2023 candidates: {cand_total}   (throughput ceiling = "
          f"{settings.optimizer.max_swaps_per_month}/mo x {settings.optimizer.horizon_months}mo "
          f"= {settings.optimizer.max_swaps_per_month * settings.optimizer.horizon_months})")
    print(f"b2-s2-r08 base load: {row_base_loads(fleet).get('b2-s2-r08'):.1f} kW")

    # ---- 1. Reproduce reported numbers ----
    print("\n" + "=" * 78)
    print("STEP 1 — reproduce reported overage")
    for hr in (0.20, 0.45):
        _, res = solve(settings, fleet, hr, penalty=1000)
        print(f"  headroom={hr:.2f}: status={res.status.value} swaps={res.total_swaps} "
              f"total_overage={res.total_row_overage_kw} "
              f"(per-month={res.total_row_overage_kw / res.horizon_months:.2f}) "
              f"flat={len(set(overage_profile(res))) == 1}")

    # ---- 2. Structural breakdown at 0.45 ----
    print("\n" + "=" * 78)
    print(f"STEP 2 — structural breakdown @ headroom=0.45 (usable row = {usable_045:.1f} kW)")
    eff, res045 = solve(settings, fleet, 0.45, penalty=1000)
    struct_total, rows = structural_overage(fleet, eff)
    print(f"  solved per-month overage  : {res045.total_row_overage_kw / res045.horizon_months:.2f} kW")
    print(f"  structural (zero-swap) min: {struct_total:.2f} kW  over {len(rows)} rows")
    print(f"  match? {abs(struct_total - res045.total_row_overage_kw / res045.horizon_months) < 1e-3}")
    print(f"  per-month overage profile : {overage_profile(res045)}")
    print("\n  Top contributing rows (load decomposition):")
    print(f"  {'row':<12} {'base':>7} {'over':>7} {'swappable':>10} {'non-removable':>14}")
    for d in rows[:12]:
        print(f"  {d['row']:<12} {d['base']:>7.1f} {d['over']:>7.1f} "
              f"{d['cand_kw']:>10.1f} {d['noncand_kw']:>14.1f}")
    tot_cand = sum(d["cand_kw"] for d in rows)
    tot_noncand = sum(d["noncand_kw"] for d in rows)
    print(f"  ... {len(rows)} rows total. In over-threshold rows: "
          f"swappable={tot_cand:.1f} kW, non-removable={tot_noncand:.1f} kW")
    # How many over-threshold rows are over purely from non-candidate load?
    purely_noncand = sum(1 for d in rows if (d["noncand_kw"] - usable_045) > 1e-9)
    print(f"  rows already over usable from NON-candidate load alone: {purely_noncand}/{len(rows)}")

    # ---- 3a. penalty 1000 vs 0 on the real fleet ----
    print("\n" + "=" * 78)
    print("STEP 3a — penalty 1000 vs 0 on the seeded fleet @ headroom=0.45")
    eff0, res0 = solve(settings, fleet, 0.45, penalty=0)
    s1000, s0 = swap_set(res045), swap_set(res0)
    print(f"  penalty=1000: swaps={res045.total_swaps} total_overage={res045.total_row_overage_kw} "
          f"profile={overage_profile(res045)}")
    print(f"  penalty=0   : swaps={res0.total_swaps} total_overage={res0.total_row_overage_kw} "
          f"profile={overage_profile(res0)}")
    print(f"  identical swap placement? {s1000 == s0}  "
          f"(symmetric diff = {len(s1000 ^ s0)} placements)")
    # positions swapped in either run that sit in an over-threshold row
    hot_rows = {d["row"] for d in rows}
    def hot_swaps(sset):
        return sum(1 for (pid, _m) in sset if any(pid.startswith(r) for r in hot_rows))
    print(f"  swaps landing in an over-threshold row: penalty1000={hot_swaps(s1000)} "
          f"penalty0={hot_swaps(s0)}")

    # ---- 3b. deterministic micro-fleet isolating placement ----
    print("\n" + "=" * 78)
    print("STEP 3b — micro-fleet: one row, swapping a candidate PUSHES it over")
    micro = micro_fleet(settings)
    effm1, rm1 = solve(settings, micro, 0.20, penalty=1000, tlimit=10)
    effm0, rm0 = solve(settings, micro, 0.20, penalty=0, tlimit=10)
    print(f"  row base=64.5 kW, usable=72 kW; swapping ai-2023->ai-2025 adds +26 kW (-> 90.5)")
    print(f"  penalty=1000: swaps={rm1.total_swaps} placed_month="
          f"{[s.month for p in rm1.plan for s in p.swaps]} total_overage={rm1.total_row_overage_kw}")
    print(f"  penalty=0   : swaps={rm0.total_swaps} placed_month="
          f"{[s.month for p in rm0.plan for s in p.swaps]} total_overage={rm0.total_row_overage_kw}")
    print(f"  objective penalty1000={rm1.objective_value:.1f}  penalty0={rm0.objective_value:.1f}")


def _r(settings, rid, row, rtype):
    spec = settings.rack_catalog[rtype]
    return Rack(rack_id=rid, position_id=f"{row}-{rid}", row_id=row, suite_id="b1-s1",
                building_id="b1", rack_type=rtype, family=spec.family,
                generation=spec.generation, power_draw_kw=spec.power_draw_kw,
                state=RackState.ACTIVE, installed_at=NOW, updated_at=NOW)


def micro_fleet(settings) -> FleetInput:
    # base = ai-2024(35) + storage-2024(7.5) + ai-2023(22) = 64.5 < 72 usable.
    # swapping the ai-2023 candidate -> ai-2025(48) adds +26 -> 90.5 (over by 18.5).
    racks = [
        _r(settings, "nc1", "b1-s1-r01", "ai-2024"),
        _r(settings, "nc2", "b1-s1-r01", "storage-2024"),
        _r(settings, "c1", "b1-s1-r01", "ai-2023"),
    ]
    caps = TierCapacities(
        building_kw={"b1": settings.power.building_capacity_mw * 1000.0},
        suite_kw={"b1-s1": settings.power.suite_capacity_mw * 1000.0},
        row_kw={"b1-s1-r01": settings.power.row_capacity_kw},
    )
    return FleetInput(racks=racks, capacities=caps)


if __name__ == "__main__":
    main()
