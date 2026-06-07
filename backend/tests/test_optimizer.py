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
from floorcast.services.optimization_service import resolve_settings


def _now():
    return datetime.now(timezone.utc)


# Optimizer-MECHANICS tests (soft-row headroom, budgets, calendar, time-weighting)
# pin a "classic" world so they don't depend on the standard multi-target floor:
# one replacement target per family (the '-value' variant) and only 2023 retirable.
# This reproduces the single-target semantics those tests were written against.
_CLASSIC = {
    "optimizer": {"retire_generation_at_or_below": 2023},
    "rack_catalog": {
        "compute-2025-max": {"is_replacement_target": False},
        "compute-2025-eff": {"is_replacement_target": False},
        "storage-2025-max": {"is_replacement_target": False},
        "storage-2025-eff": {"is_replacement_target": False},
        "ai-2025-max": {"is_replacement_target": False},
        "ai-2025-eff": {"is_replacement_target": False},
    },
}
# The sole compute target in the classic world (used by budget arithmetic).
_CLASSIC_COMPUTE_TARGET = "compute-2025-value"


@pytest.fixture
def classic_settings(settings):
    return resolve_settings(settings, _CLASSIC)


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


def test_scheduled_swaps_carry_correct_row_id(classic_settings):
    settings = classic_settings
    # Every swap must know which row it lands in (persisted onto schedule_items),
    # sourced from the fleet's row identity — never null, never parsed later.
    # _hot_row_fleet deterministically yields the cool 2023 candidate as a swap.
    fleet, _ = _hot_row_fleet(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="rowid", created_at=datetime.now(timezone.utc))

    pos_to_row = {r.position_id: r.row_id for r in fleet.racks}
    swaps = [swap for plan in result.plan for swap in plan.swaps]
    assert swaps, "expected the fixture floor to produce at least one swap"
    for swap in swaps:
        assert swap.row_id, f"swap at {swap.position_id} has no row_id"
        assert swap.row_id == pos_to_row[swap.position_id]


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


def test_hot_row_is_feasible_with_soft_headroom(classic_settings):
    settings = classic_settings
    # Before soft row headroom this floor was INFEASIBLE (the hot row breaks the
    # row threshold at month 0). It must now solve.
    fleet, _ = _hot_row_fleet(settings)
    optimizer = RackReplacementOptimizer(settings)
    result = optimizer.solve(fleet, run_id="hot1", created_at=datetime.now(timezone.utc))

    assert result.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    # Swaps still happen alongside the unavoidable overage (the cool candidate).
    assert result.total_swaps >= 1


def test_row_overage_is_reported_and_minimized(classic_settings):
    settings = classic_settings
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


# --------------------------------------------------------------------------- #
# Stage 2: target-choice (y[i,g,t]), selectable objective, budget constraint.
# --------------------------------------------------------------------------- #
def _independent_candidates(settings, n, rack_type="compute-2023", building_cap_mw=6.0):
    """n retiring racks, each alone in its own (cool, ample) row in one suite.

    Independent so the only thing limiting swaps is throughput/budget, not power.
    """
    now = _now()
    spec = settings.rack_catalog[rack_type]
    racks, row_kw = [], {}
    for k in range(n):
        rid = f"b1-s1-r{k:02d}"
        racks.append(
            Rack(
                rack_id=f"c{k}", position_id=f"{rid}-p1", row_id=rid,
                suite_id="b1-s1", building_id="b1", rack_type=rack_type,
                family=spec.family, generation=spec.generation,
                power_draw_kw=spec.power_draw_kw, state=RackState.ACTIVE,
                installed_at=now, updated_at=now,
            )
        )
        row_kw[rid] = settings.power.row_capacity_kw
    caps = TierCapacities(
        building_kw={"b1": building_cap_mw * 1000.0},
        suite_kw={"b1-s1": settings.power.suite_capacity_mw * 1000.0},
        row_kw=row_kw,
    )
    return FleetInput(racks=racks, capacities=caps)


def test_default_behaviour_unchanged_single_target(classic_settings):
    settings = classic_settings
    # With one target per family, y[i,g,t] collapses to the old y[i,t] behaviour:
    # exactly one swap, to the sole family target, chosen target reported correctly.
    fleet, expected_overage = _hot_row_fleet(settings)
    result = RackReplacementOptimizer(settings).solve(
        fleet, run_id="reg", created_at=_now()
    )
    H = settings.optimizer.horizon_months

    assert result.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert result.total_swaps == 1
    assert result.total_row_overage_kw == pytest.approx(expected_overage * H, abs=1e-3)

    swaps = [s for plan in result.plan for s in plan.swaps]
    assert len(swaps) == 1
    # The chosen target is reported from the solution, not a frozen tuple.
    assert swaps[0].from_rack_type == "compute-2023"
    assert swaps[0].to_rack_type == _CLASSIC_COMPUTE_TARGET
    assert swaps[0].to_power_kw == settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].power_draw_kw


def test_budget_cap_limits_swaps_and_spend(classic_settings):
    settings = classic_settings
    fleet = _independent_candidates(settings, n=5)
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost

    # No budget -> all five swap.
    res0 = RackReplacementOptimizer(settings).solve(fleet, run_id="b0", created_at=_now())
    assert res0.total_swaps == 5

    # Budget for exactly two swaps (2.5 * cost -> floor is 2).
    eff = resolve_settings(settings, {"optimizer": {"budget_cap": 2.5 * cost}})
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="b1", created_at=_now())
    assert res.total_swaps == 2

    spend = sum(
        settings.rack_catalog[s.to_rack_type].cost
        for plan in res.plan
        for s in plan.swaps
    )
    assert spend <= 2.5 * cost


def test_budget_too_low_yields_zero_swaps_not_infeasible(classic_settings):
    settings = classic_settings
    # A cap below the cheapest swap leaves the model feasible with zero swaps.
    eff = resolve_settings(settings, {"optimizer": {"budget_cap": 1.0}})
    fleet = _independent_candidates(eff, n=3)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="z", created_at=_now())
    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert res.total_swaps == 0


def test_multi_target_choice_keeps_single_target_invariant(settings):
    # The standard catalog has THREE compute targets; the solver may pick any, but
    # each rack still ends up with at most ONE target/swap.
    compute_targets = {
        k for k, v in settings.replacement_targets().items() if v.family == "compute"
    }
    assert compute_targets == {"compute-2025-value", "compute-2025-max", "compute-2025-eff"}

    fleet = _independent_candidates(settings, n=3)
    res = RackReplacementOptimizer(settings).solve(fleet, run_id="c1", created_at=_now())

    assert res.total_swaps == 3
    seen = [s.position_id for plan in res.plan for s in plan.swaps]
    assert len(seen) == len(set(seen)), "a position was swapped more than once"
    for plan in res.plan:
        for s in plan.swaps:
            assert s.to_rack_type in compute_targets


def test_budget_forces_optimizer_to_choose_cheaper_target(settings):
    # compute targets cost value 40000 < eff 60000 < max 90000. A cap of 50000
    # only fits the cheapest, so to earn the completion reward the solver must
    # CHOOSE compute-2025-value.
    eff = resolve_settings(settings, {"optimizer": {"budget_cap": 50000}})
    fleet = _independent_candidates(eff, n=1)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="c2", created_at=_now())

    assert res.total_swaps == 1
    chosen = [s.to_rack_type for plan in res.plan for s in plan.swaps]
    assert chosen == ["compute-2025-value"]


def test_clean_infeasible_when_base_load_exceeds_building_headroom(settings):
    # Base load already over the (tiny) building budget -> hard constraint can't
    # hold even at zero swaps. Must report INFEASIBLE cleanly, not crash.
    fleet = _independent_candidates(settings, n=3, building_cap_mw=0.0001)
    res = RackReplacementOptimizer(settings).solve(fleet, run_id="inf", created_at=_now())
    assert res.status == RunStatus.INFEASIBLE
    assert res.plan == []
    assert res.total_swaps == 0


# --------------------------------------------------------------------------- #
# Stage 3: calendar layer + per-quarter budget constraints.
# --------------------------------------------------------------------------- #
def test_calendar_from_range_and_partial_quarters():
    from floorcast.optimizer.calendar import abstract_calendar, calendar_from_range

    steps = calendar_from_range(2026, 5, 2027, 2)  # May 2026 .. Feb 2027
    assert len(steps) == 10
    assert (steps[0].label, steps[0].quarter) == ("2026-05", "2026-Q2")
    assert (steps[-1].label, steps[-1].quarter) == ("2027-02", "2027-Q1")
    # First quarter is partial: only May, Jun fall in range for Q2.
    q2 = sorted(s.month for s in steps if s.quarter == "2026-Q2")
    assert q2 == [5, 6]

    with pytest.raises(ValueError):
        calendar_from_range(2027, 2, 2026, 5)  # end precedes start

    ab = abstract_calendar(12)
    assert len(ab) == 12
    assert all(s.quarter is None and s.year is None for s in ab)
    assert ab[0].label == "1"


def test_default_run_has_no_calendar_or_quarters(settings):
    # REGRESSION: with no date range, plans carry no calendar labels and there
    # are no quarters; monthly_spend is keyed by abstract step numbers.
    fleet = _fleet_from_floor(settings)
    res = RackReplacementOptimizer(settings).solve(fleet, run_id="d", created_at=_now())
    assert res.quarterly_spend == {}
    for plan in res.plan:
        assert plan.quarter is None and plan.year is None
        assert plan.label == str(plan.month)
    H = settings.optimizer.horizon_months
    assert set(res.monthly_spend) == {str(t) for t in range(1, H + 1)}


def test_calendar_range_matches_equal_length_abstract_run(settings):
    # REGRESSION (b): a date range only RELABELS months; for equal length it must
    # not change which/how many swaps happen.
    abstract = resolve_settings(settings, {"optimizer": {"horizon_months": 10}})
    calendar = resolve_settings(
        settings,
        {"optimizer": {"calendar": {
            "start_year": 2026, "start_month": 5,
            "end_year": 2027, "end_month": 2,  # 10 months
        }}},
    )
    fleet_a = _fleet_from_floor(abstract)
    fleet_c = _fleet_from_floor(calendar)

    res_a = RackReplacementOptimizer(abstract).solve(fleet_a, run_id="a", created_at=_now())
    res_c = RackReplacementOptimizer(calendar).solve(fleet_c, run_id="c", created_at=_now())

    assert res_c.horizon_months == res_a.horizon_months == 10
    assert res_c.total_swaps == res_a.total_swaps
    assert res_c.total_row_overage_kw == pytest.approx(res_a.total_row_overage_kw, abs=1e-3)
    # Calendar run is labelled; abstract run is not.
    assert res_c.plan[0].quarter == "2026-Q2"
    assert res_a.plan[0].quarter is None


def test_per_quarter_cap_limits_each_quarter_spend(classic_settings):
    settings = classic_settings
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "calendar": {"start_year": 2026, "start_month": 1,
                         "end_year": 2026, "end_month": 12},  # full year, Q1..Q4
            "per_quarter_budget_cap": 2.5 * cost,  # -> at most 2 swaps/quarter
        }},
    )
    fleet = _independent_candidates(eff, n=12)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="q", created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert set(res.quarterly_spend) == {"2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"}
    for spend in res.quarterly_spend.values():
        assert spend <= 2.5 * cost + 1e-6
    assert res.total_swaps <= 8  # 4 quarters * 2 swaps


def test_partial_quarter_assigns_only_in_range_months(classic_settings):
    settings = classic_settings
    eff = resolve_settings(
        settings,
        {"optimizer": {"calendar": {
            "start_year": 2026, "start_month": 5,
            "end_year": 2026, "end_month": 8,  # May..Aug -> Q2 (May,Jun) + Q3 (Jul,Aug)
        }}},
    )
    fleet = _independent_candidates(eff, n=2)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="pq", created_at=_now())

    assert {p.quarter for p in res.plan} == {"2026-Q2", "2026-Q3"}
    assert sorted(p.calendar_month for p in res.plan if p.quarter == "2026-Q2") == [5, 6]
    assert sorted(p.calendar_month for p in res.plan if p.quarter == "2026-Q3") == [7, 8]


def test_total_and_per_quarter_caps_both_respected(classic_settings):
    settings = classic_settings
    # The "May-Feb, £total AND <=£/quarter" example.
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost
    total_cap = 5.5 * cost
    pq_cap = 2.5 * cost
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "calendar": {"start_year": 2026, "start_month": 5,
                         "end_year": 2027, "end_month": 2},
            "budget_cap": total_cap,
            "per_quarter_budget_cap": pq_cap,
        }},
    )
    fleet = _independent_candidates(eff, n=12)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="combo", created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert sum(res.monthly_spend.values()) <= total_cap + 1e-6
    for spend in res.quarterly_spend.values():
        assert spend <= pq_cap + 1e-6
    assert res.total_swaps <= 5  # total cap binds first (5.5 * cost)


def test_too_tight_per_quarter_cap_degrades_not_infeasible(classic_settings):
    settings = classic_settings
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "calendar": {"start_year": 2026, "start_month": 1,
                         "end_year": 2026, "end_month": 12},
            "per_quarter_budget_cap": 1.0,  # below any single swap cost
        }},
    )
    fleet = _independent_candidates(eff, n=4)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="tight", created_at=_now())
    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert res.total_swaps == 0


def test_per_quarter_cap_without_calendar_raises(classic_settings):
    settings = classic_settings
    # Quarters only exist in calendar mode; a per-quarter cap without a date
    # range is a misconfiguration, surfaced clearly (not silently ignored).
    eff = resolve_settings(settings, {"optimizer": {"per_quarter_budget_cap": 100000}})
    fleet = _independent_candidates(eff, n=2)
    with pytest.raises(ValueError):
        RackReplacementOptimizer(eff).solve(fleet, run_id="bad", created_at=_now())


# --------------------------------------------------------------------------- #
# Stage 4: value-aware sequencing (time-weighting) + minimize_headroom_violation.
# --------------------------------------------------------------------------- #
def _mk_rack(settings, rid, rack_id, rtype):
    spec = settings.rack_catalog[rtype]
    now = _now()
    return Rack(
        rack_id=rack_id, position_id=f"{rid}-{rack_id}", row_id=rid,
        suite_id="b1-s1", building_id="b1", rack_type=rtype,
        family=spec.family, generation=spec.generation,
        power_draw_kw=spec.power_draw_kw, state=RackState.ACTIVE,
        installed_at=now, updated_at=now,
    )


def _hot_row_with_candidate(settings):
    """One near-threshold row: 61 kW of non-candidate (2024) fillers + a single
    2023 candidate (8.5 kW). base = 69.5 <= usable 72, but swapping the candidate
    to its sole classic target (compute-2025-value, 12.0) pushes the row to 73.0
    -> +1.0 kW overage. So this candidate's swap *creates* a headroom violation,
    while a swap elsewhere would not. (Used under classic_settings: the 2024
    fillers are non-candidate because retire<=2023.)"""
    return [
        _mk_rack(settings, "b1-s1-r01", "f-ai", "ai-2024"),       # 35.0
        _mk_rack(settings, "b1-s1-r01", "f-cm", "compute-2024"),  # 11.0
        _mk_rack(settings, "b1-s1-r01", "f-st1", "storage-2024"), # 7.5
        _mk_rack(settings, "b1-s1-r01", "f-st2", "storage-2024"), # 7.5
        _mk_rack(settings, "b1-s1-r01", "cand-hot", "compute-2023"),  # 8.5 -> 13.5
    ]


def _caps(settings, rows):
    return TierCapacities(
        building_kw={"b1": settings.power.building_capacity_mw * 1000.0},
        suite_kw={"b1-s1": settings.power.suite_capacity_mw * 1000.0},
        row_kw={rid: settings.power.row_capacity_kw for rid in rows},
    )


def _overage_causing_fleet(settings):
    # near-threshold row (swap creates overage) + a cool row (swap is safe).
    racks = _hot_row_with_candidate(settings)
    racks.append(_mk_rack(settings, "b1-s1-r02", "cand-cool", "compute-2023"))
    return FleetInput(racks=racks, capacities=_caps(settings, ["b1-s1-r01", "b1-s1-r02"]))


def _single_hot_candidate_fleet(settings):
    racks = _hot_row_with_candidate(settings)
    return FleetInput(racks=racks, capacities=_caps(settings, ["b1-s1-r01"]))


def _only_swap_month(res):
    swaps = [s for plan in res.plan for s in plan.swaps]
    assert len(swaps) == 1, f"expected exactly one swap, got {len(swaps)}"
    return swaps[0].month


def test_default_time_discount_is_inert(classic_settings):
    settings = classic_settings
    # time_discount defaults to 1.0, so w(t)==1 and the completion reward's w(t)
    # factor collapses to the historical -big_m * done.
    assert settings.optimizer.time_discount == 1.0
    fleet, expected_overage = _hot_row_fleet(settings)
    res = RackReplacementOptimizer(settings).solve(fleet, run_id="inert", created_at=_now())
    H = settings.optimizer.horizon_months
    assert res.total_swaps == 1
    assert res.total_row_overage_kw == pytest.approx(expected_overage * H, abs=1e-3)


def test_minimize_headroom_violation_changes_plan(classic_settings):
    settings = classic_settings
    fleet = _overage_causing_fleet(settings)

    res_racks = RackReplacementOptimizer(settings).solve(
        fleet, run_id="mr", created_at=_now()
    )
    eff = resolve_settings(settings, {"optimizer": {"objective": "minimize_headroom_violation"}})
    res_head = RackReplacementOptimizer(eff).solve(fleet, run_id="mh", created_at=_now())

    assert res_racks.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert res_head.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)

    # maximize_racks_modernized does BOTH swaps and accepts the hot row's overage.
    assert res_racks.total_swaps == 2
    assert res_racks.total_row_overage_kw > 0.0
    # minimize_headroom_violation skips the harmful swap -> strictly less overage.
    assert res_head.total_swaps == 1
    assert res_head.total_row_overage_kw == pytest.approx(0.0, abs=1e-6)
    assert res_head.total_row_overage_kw < res_racks.total_row_overage_kw


def test_time_discount_schedules_value_earlier(classic_settings):
    settings = classic_settings
    # Isolate timing: drop the deferral tie-breaker so the only timing forces are
    # the (discountable) completion reward vs the overage penalty (which prefers
    # LATE -> fewer kW-months). One candidate whose swap creates overage.
    fleet = _single_hot_candidate_fleet(settings)
    H = settings.optimizer.horizon_months

    off = resolve_settings(
        settings, {"optimizer": {"deferral_penalty_per_month": 0.0, "time_discount": 1.0}}
    )
    on = resolve_settings(
        settings, {"optimizer": {"deferral_penalty_per_month": 0.0, "time_discount": 0.95}}
    )
    res_off = RackReplacementOptimizer(off).solve(fleet, run_id="toff", created_at=_now())
    res_on = RackReplacementOptimizer(on).solve(fleet, run_id="ton", created_at=_now())

    month_off = _only_swap_month(res_off)
    month_on = _only_swap_month(res_on)

    # No discount: overage pulls the swap to the LAST month. Discount on: the
    # decaying completion reward pulls it to the FIRST month.
    assert month_off == H
    assert month_on == 1
    assert month_on < month_off


def test_unknown_objective_raises(settings):
    # Every recognized objective is implemented; an unknown name is a clean error.
    fleet = _fleet_from_floor(settings)
    bad = resolve_settings(settings, {"optimizer": {"objective": "frobnicate"}})
    with pytest.raises(ValueError):
        RackReplacementOptimizer(bad).solve(fleet, run_id="badobj", created_at=_now())


# --------------------------------------------------------------------------- #
# Stage 5: the three value objectives. The standard config IS the multi-target
# catalog now, so demo_settings is just the default settings.
# --------------------------------------------------------------------------- #
@pytest.fixture
def demo_settings(settings):
    return settings


def _demo_value_fleet(s):
    """Three candidates (one per family), each alone in an ample, cool row so NO
    target choice causes overage -> the value term alone drives target selection."""
    now = _now()

    def mk(rid, rtype):
        spec = s.rack_catalog[rtype]
        return Rack(
            rack_id=f"rk-{rid}", position_id=f"{rid}-p0", row_id=rid,
            suite_id="b1-s1", building_id="b1", rack_type=rtype,
            family=spec.family, generation=spec.generation,
            power_draw_kw=spec.power_draw_kw, state=RackState.ACTIVE,
            installed_at=now, updated_at=now,
        )

    racks = [
        mk("b1-s1-r01", "ai-2023"),
        mk("b1-s1-r02", "compute-2023"),
        mk("b1-s1-r03", "storage-2023"),
    ]
    caps = TierCapacities(
        building_kw={"b1": 6000.0}, suite_kw={"b1-s1": 1500.0},
        row_kw={"b1-s1-r01": 90.0, "b1-s1-r02": 90.0, "b1-s1-r03": 90.0},
    )
    return FleetInput(racks=racks, capacities=caps)


# Expected argmax target per family for each objective (from the demo target table).
_ARGMAX_TARGET = {
    "maximize_value_per_pound": {
        "ai": "ai-2025-value", "compute": "compute-2025-value", "storage": "storage-2025-value",
    },
    "maximize_compute": {
        "ai": "ai-2025-max", "compute": "compute-2025-max", "storage": "storage-2025-max",
    },
    "maximize_efficiency": {
        "ai": "ai-2025-eff", "compute": "compute-2025-eff", "storage": "storage-2025-eff",
    },
}


@pytest.mark.parametrize("objective", sorted(_ARGMAX_TARGET))
def test_value_objective_picks_argmax_target(demo_settings, objective):
    # The REAL proof: each objective installs its argmax target per family.
    eff = resolve_settings(demo_settings, {"optimizer": {"objective": objective}})
    fleet = _demo_value_fleet(eff)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id=objective, created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    chosen = {}
    for plan in res.plan:
        for sw in plan.swaps:
            chosen[sw.to_rack_type.split("-")[0]] = sw.to_rack_type  # family prefix
    assert chosen == _ARGMAX_TARGET[objective]


def test_value_objectives_diverge_on_demo_floor(demo_settings):
    from floorcast.services.generator import generate_demo_floor

    floor = generate_demo_floor(demo_settings)
    caps = TierCapacities()
    for b in floor.buildings:
        caps.building_kw[b.building_id] = b.capacity_mw * 1000.0
        for su in b.suites:
            caps.suite_kw[su.suite_id] = su.capacity_mw * 1000.0
            for row in su.rows:
                caps.row_kw[row.row_id] = row.capacity_kw
    fleet = FleetInput(racks=floor.racks, capacities=caps)

    objectives = [
        "maximize_racks_modernized",
        "maximize_value_per_pound",
        "maximize_compute",
        "maximize_efficiency",
    ]
    plans = {}
    for o in objectives:
        eff = resolve_settings(demo_settings, {"optimizer": {"objective": o}})
        res = RackReplacementOptimizer(eff).solve(fleet, run_id=o, created_at=_now())
        assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
        plans[o] = frozenset(
            (sw.position_id, sw.to_rack_type, sw.month)
            for p in res.plan for sw in p.swaps
        )

    # All four plans differ.
    assert len(set(plans.values())) == len(objectives)

    # Each value objective demonstrably reaches for its argmax variant.
    def used(o):
        return {t for (_p, t, _m) in plans[o]}

    assert any(t.endswith("-value") for t in used("maximize_value_per_pound"))
    assert any(t.endswith("-max") for t in used("maximize_compute"))
    assert any(t.endswith("-eff") for t in used("maximize_efficiency"))


# --------------------------------------------------------------------------- #
# Consolidation: the single standard floor's known-good baseline.
# --------------------------------------------------------------------------- #
def _fleet_from_generated(floor) -> FleetInput:
    caps = TierCapacities()
    for b in floor.buildings:
        caps.building_kw[b.building_id] = b.capacity_mw * 1000.0
        for su in b.suites:
            caps.suite_kw[su.suite_id] = su.capacity_mw * 1000.0
            for row in su.rows:
                caps.row_kw[row.row_id] = row.capacity_kw
    return FleetInput(racks=floor.racks, capacities=caps)


def test_standard_floor_default_objective_baseline(settings):
    """REGRESSION ANCHOR (replaces the old 96 / 42 default-floor baseline).

    The standard seeded floor (generate_demo_floor) under the default objective
    maximize_racks_modernized with NO overrides must reproduce exactly this
    result. This is the new must-not-drift number.
    """
    from floorcast.services.generator import generate_demo_floor

    assert settings.optimizer.objective == "maximize_racks_modernized"
    fleet = _fleet_from_generated(generate_demo_floor(settings))
    res = RackReplacementOptimizer(settings).solve(
        fleet, run_id="baseline", created_at=_now()
    )
    assert res.status == RunStatus.OPTIMAL
    assert res.total_swaps == 96
    assert res.total_row_overage_kw == pytest.approx(612.0, abs=1e-3)


# --------------------------------------------------------------------------- #
# Count constraints: per-target "at least N" / "at most N" installs.
# --------------------------------------------------------------------------- #
def _standard_fleet(settings):
    from floorcast.services.generator import generate_demo_floor

    return _fleet_from_generated(generate_demo_floor(settings))


def _count_by_target(res):
    counts: dict[str, int] = {}
    for plan in res.plan:
        for sw in plan.swaps:
            counts[sw.to_rack_type] = counts.get(sw.to_rack_type, 0) + 1
    return counts


def _total_spend(settings, res):
    return sum(
        settings.rack_catalog[sw.to_rack_type].cost
        for plan in res.plan for sw in plan.swaps
    )


def test_empty_count_constraints_are_inert(settings):
    # Default: no count constraints -> identical to the regression baseline.
    assert settings.optimizer.count_constraints == {}
    res = RackReplacementOptimizer(settings).solve(
        _standard_fleet(settings), run_id="cc-inert", created_at=_now()
    )
    assert res.status == RunStatus.OPTIMAL
    assert res.total_swaps == 96
    assert res.total_row_overage_kw == pytest.approx(612.0, abs=1e-3)


def test_count_min_installs_at_least_n(settings):
    # Default objective avoids the power-hungry ai-2025-max (overage); a min forces
    # at least N of it to be installed anyway.
    eff = resolve_settings(
        settings, {"optimizer": {"count_constraints": {"ai-2025-max": {"min": 10}}}}
    )
    res = RackReplacementOptimizer(eff).solve(
        _standard_fleet(eff), run_id="cc-min", created_at=_now()
    )
    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert _count_by_target(res).get("ai-2025-max", 0) >= 10


def test_count_max_caps_and_binds(settings):
    # maximize_compute would otherwise over-install ai-2025-max (highest compute);
    # a max cap must bind: fewer than unconstrained, and never above the cap.
    base = resolve_settings(settings, {"optimizer": {"objective": "maximize_compute"}})
    fleet = _standard_fleet(base)
    res_free = RackReplacementOptimizer(base).solve(fleet, run_id="cc-free", created_at=_now())
    free_max = _count_by_target(res_free).get("ai-2025-max", 0)
    assert free_max > 5, "fixture expects maximize_compute to over-install ai-2025-max"

    capped = resolve_settings(
        settings,
        {"optimizer": {
            "objective": "maximize_compute",
            "count_constraints": {"ai-2025-max": {"max": 5}},
        }},
    )
    res = RackReplacementOptimizer(capped).solve(fleet, run_id="cc-max", created_at=_now())
    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    capped_max = _count_by_target(res).get("ai-2025-max", 0)
    assert capped_max <= 5
    assert capped_max < free_max


def test_count_max_only_never_infeasible(settings):
    # A max (even 0) can always be satisfied by installing fewer -> still feasible.
    eff = resolve_settings(
        settings, {"optimizer": {"count_constraints": {"ai-2025-max": {"max": 0}}}}
    )
    res = RackReplacementOptimizer(eff).solve(
        _standard_fleet(eff), run_id="cc-max0", created_at=_now()
    )
    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert _count_by_target(res).get("ai-2025-max", 0) == 0
    assert res.total_swaps > 0  # other targets still get installed


def test_count_min_unsatisfiable_is_clean_infeasible(settings):
    # Far more than the available ai candidates (44) and the throughput cap (96).
    eff = resolve_settings(
        settings, {"optimizer": {"count_constraints": {"ai-2025-max": {"min": 200}}}}
    )
    res = RackReplacementOptimizer(eff).solve(
        _standard_fleet(eff), run_id="cc-inf", created_at=_now()
    )
    assert res.status == RunStatus.INFEASIBLE
    assert res.plan == []
    assert res.total_swaps == 0


def test_count_min_greater_than_max_raises(settings):
    eff = resolve_settings(
        settings,
        {"optimizer": {"count_constraints": {"compute-2025-value": {"min": 5, "max": 2}}}},
    )
    with pytest.raises(ValueError):
        RackReplacementOptimizer(eff).solve(
            _standard_fleet(eff), run_id="cc-bad1", created_at=_now()
        )


def test_count_bad_target_raises(settings):
    # A non-target rack type (current fleet) and an unknown type both error.
    for bad in ("compute-2023", "totally-made-up"):
        eff = resolve_settings(
            settings, {"optimizer": {"count_constraints": {bad: {"min": 1}}}}
        )
        with pytest.raises(ValueError):
            RackReplacementOptimizer(eff).solve(
                _standard_fleet(eff), run_id="cc-bad2", created_at=_now()
            )


def test_count_constraint_stacks_with_budget_and_value_objective(settings):
    # maximize_compute + total budget cap + "at least 5 of ai-2025-eff", all at once.
    budget = 4_000_000.0
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "objective": "maximize_compute",
            "budget_cap": budget,
            "count_constraints": {"ai-2025-eff": {"min": 5}},
        }},
    )
    res = RackReplacementOptimizer(eff).solve(
        _standard_fleet(eff), run_id="cc-stack", created_at=_now()
    )
    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert _count_by_target(res).get("ai-2025-eff", 0) >= 5      # count min respected
    assert _total_spend(settings, res) <= budget + 1e-6          # budget respected


# --------------------------------------------------------------------------- #
# Unified budget caps: per-period (total / year / quarter / month), stackable.
# --------------------------------------------------------------------------- #
def test_per_month_cap_limits_each_month_abstract(classic_settings):
    # period=month works WITHOUT a calendar (abstract-month mode).
    settings = classic_settings
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost
    eff = resolve_settings(
        settings,
        {"optimizer": {"budget_caps": [{"period": "month", "cap": 1.5 * cost}]}},  # <=1/mo
    )
    fleet = _independent_candidates(eff, n=5)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="bm", created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    # abstract mode -> no quarter/year buckets
    assert res.quarterly_spend == {} and res.yearly_spend == {}
    for v in res.monthly_spend.values():
        assert v <= 1.5 * cost + 1e-6
    assert res.total_swaps == 5  # all done, spread one per month


def test_per_year_cap_requires_calendar_and_limits_each_year(classic_settings):
    settings = classic_settings
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "calendar": {"start_year": 2026, "start_month": 1,
                         "end_year": 2027, "end_month": 12},  # 24 months, 2 years
            "budget_caps": [{"period": "year", "cap": 2.5 * cost}],  # <=2 swaps/year
        }},
    )
    fleet = _independent_candidates(eff, n=6)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="by", created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert set(res.yearly_spend) == {"2026", "2027"}
    for v in res.yearly_spend.values():
        assert v <= 2.5 * cost + 1e-6
    assert res.total_swaps <= 4


def test_year_and_quarter_caps_without_calendar_raise(classic_settings):
    # quarter/year periods only exist in calendar mode -> clean ValueError.
    settings = classic_settings
    for period in ("year", "quarter"):
        eff = resolve_settings(
            settings, {"optimizer": {"budget_caps": [{"period": period, "cap": 100000}]}}
        )
        fleet = _independent_candidates(eff, n=2)
        with pytest.raises(ValueError):
            RackReplacementOptimizer(eff).solve(fleet, run_id="nocal", created_at=_now())


def test_stacked_total_quarter_month_caps_all_respected(classic_settings):
    settings = classic_settings
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost
    total_cap, quarter_cap, month_cap = 6 * cost, 2.5 * cost, 1.5 * cost
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "calendar": {"start_year": 2026, "start_month": 1,
                         "end_year": 2026, "end_month": 12},
            "budget_caps": [
                {"period": "total", "cap": total_cap},
                {"period": "quarter", "cap": quarter_cap},
                {"period": "month", "cap": month_cap},
            ],
        }},
    )
    fleet = _independent_candidates(eff, n=12)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="stack3", created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert sum(res.monthly_spend.values()) <= total_cap + 1e-6
    for v in res.quarterly_spend.values():
        assert v <= quarter_cap + 1e-6
    for v in res.monthly_spend.values():
        assert v <= month_cap + 1e-6


def test_sugar_and_list_budget_caps_coexist(classic_settings):
    # The back-compat scalar field (budget_cap -> total) and the new list both apply.
    settings = classic_settings
    cost = settings.rack_catalog[_CLASSIC_COMPUTE_TARGET].cost
    eff = resolve_settings(
        settings,
        {"optimizer": {
            "budget_cap": 3 * cost,                                   # sugar -> total
            "budget_caps": [{"period": "month", "cap": 1.5 * cost}],  # list  -> month
        }},
    )
    fleet = _independent_candidates(eff, n=10)
    res = RackReplacementOptimizer(eff).solve(fleet, run_id="coexist", created_at=_now())

    assert res.status in (RunStatus.OPTIMAL, RunStatus.FEASIBLE)
    assert sum(res.monthly_spend.values()) <= 3 * cost + 1e-6  # total (sugar) respected
    for v in res.monthly_spend.values():
        assert v <= 1.5 * cost + 1e-6                          # month (list) respected
    assert res.total_swaps == 3  # total cap binds at 3, spread one per month
