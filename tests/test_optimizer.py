"""Optimizer produces a feasible schedule.

Building- and suite-tier headroom are HARD constraints (never violated). Row-tier
headroom is SOFT: a row may exceed its threshold, but the overage is reported and
the optimizer minimizes it.
"""

from datetime import datetime, timezone

import pytest

from floorcast.models.optimization import RunStatus
from floorcast.models.rack import Rack, RackState
from floorcast.optimizer.engine import FleetInput, RackReplacementOptimizer, TierCapacities
from floorcast.services.generator import GeneratorConfig, generate_floor


def _fleet_from_floor(settings) -> FleetInput:
    # Small floor so the solver is fast and feasible.
    floor = generate_floor(
        settings,
        GeneratorConfig(buildings=1, suites_per_building=2, rows_per_suite=4,
                        positions_per_row=6, fill_ratio=0.4, seed=3),
    )
    caps = TierCapacities()
    for b in floor.buildings:
        caps.building_kw[b.building_id] = b.capacity_mw * 1000.0
        for s in b.suites:
            caps.suite_kw[s.suite_id] = s.capacity_mw * 1000.0
            for row in s.rows:
                caps.row_kw[row.row_id] = row.capacity_kw
    return FleetInput(racks=floor.racks, capacities=caps)


def test_solver_returns_schedule(settings):
    fleet = _fleet_from_floor(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="t1", created_at=datetime.now(timezone.utc))

    assert result.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert len(result.plan) == settings.optimizer.horizon_months


def test_building_and_suite_headroom_never_violated(settings):
    # Row-tier headroom is now soft, so it is intentionally NOT asserted here.
    fleet = _fleet_from_floor(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="t2", created_at=datetime.now(timezone.utc))

    usable = 1.0 - settings.power.headroom_pct
    for plan in result.plan:
        for sid, util in plan.suite_peak_util.items():
            assert util <= usable + 1e-6, f"suite {sid} exceeded headroom in month {plan.month}"
        for bid, util in plan.building_peak_util.items():
            assert util <= usable + 1e-6, f"building {bid} exceeded headroom in month {plan.month}"


def test_throughput_caps_respected(settings):
    fleet = _fleet_from_floor(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="t3", created_at=datetime.now(timezone.utc))

    for plan in result.plan:
        assert len(plan.swaps) <= settings.optimizer.max_swaps_per_month


def _rack(settings, rack_id, row_id, rack_type) -> Rack:
    spec = settings.rack_catalog[rack_type]
    now = datetime.now(timezone.utc)
    return Rack(
        rack_id=rack_id,
        position_id=f"{row_id}-{rack_id}",
        row_id=row_id,
        suite_id="b1-s1",
        building_id="b1",
        rack_type=rack_type,
        family=spec.family,
        generation=spec.generation,
        power_draw_kw=spec.power_draw_kw,
        state=RackState.ACTIVE,
        installed_at=now,
        updated_at=now,
    )


def _hot_row_fleet(settings) -> tuple[FleetInput, float]:
    """A floor with one deliberately over-headroom row of NON-candidate (2024)
    racks, plus a cool row holding a single 2023 candidate.

    The hot row's racks are all generation 2024, so no swap can touch it and its
    overage is fixed at (load - usable) every month — making the expected total
    deterministic. Returns (fleet, expected_overage_per_month).
    """
    ai = settings.rack_catalog["ai-2024"].power_draw_kw
    cm24 = settings.rack_catalog["compute-2024"].power_draw_kw
    row_usable = settings.power.usable(settings.power.row_capacity_kw)

    hot_load = 2 * ai + cm24
    assert hot_load > row_usable, "test fixture must put the hot row over headroom"
    expected_overage = hot_load - row_usable

    racks = [
        # hot row (b1-s1-r01): two AI + one compute, all 2024 -> not retirable
        _rack(settings, "hot-ai-1", "b1-s1-r01", "ai-2024"),
        _rack(settings, "hot-ai-2", "b1-s1-r01", "ai-2024"),
        _rack(settings, "hot-cm-1", "b1-s1-r01", "compute-2024"),
        # cool row (b1-s1-r02): one 2023 compute -> a replacement candidate
        _rack(settings, "cool-cm-1", "b1-s1-r02", "compute-2023"),
    ]
    caps = TierCapacities(
        building_kw={"b1": settings.power.building_capacity_mw * 1000.0},
        suite_kw={"b1-s1": settings.power.suite_capacity_mw * 1000.0},
        row_kw={
            "b1-s1-r01": settings.power.row_capacity_kw,
            "b1-s1-r02": settings.power.row_capacity_kw,
        },
    )
    return FleetInput(racks=racks, capacities=caps), expected_overage


def test_hot_row_is_feasible_with_soft_headroom(settings):
    # Before soft row headroom this floor was INFEASIBLE (the hot row breaks the
    # row threshold at month 0). It must now solve.
    fleet, _ = _hot_row_fleet(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="hot1", created_at=datetime.now(timezone.utc))

    assert result.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    # Swaps still happen alongside the unavoidable overage (the cool candidate).
    assert result.total_swaps >= 1


def test_row_overage_is_reported_and_minimized(settings):
    fleet, expected_overage = _hot_row_fleet(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="hot2", created_at=datetime.now(timezone.utc))

    H = settings.optimizer.horizon_months

    # Overage is reported per month and in aggregate.
    assert result.total_row_overage_kw == pytest.approx(expected_overage * H, abs=1e-3)
    for plan in result.plan:
        # The hot row cannot be improved (no candidate in it), so the solver
        # holds overage at exactly the unavoidable minimum every month and never
        # lets the cool row's swap push it over threshold.
        assert plan.row_overage_kw == pytest.approx(expected_overage, abs=1e-3)

    # Building/suite headroom remain hard and within bounds throughout.
    usable = 1.0 - settings.power.headroom_pct
    for plan in result.plan:
        for util in plan.suite_peak_util.values():
            assert util <= usable + 1e-6
        for util in plan.building_peak_util.values():
            assert util <= usable + 1e-6
