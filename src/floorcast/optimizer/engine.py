"""ILP rack-replacement optimizer (OR-Tools / CBC MIP).

Produces a month-by-month rack replacement schedule over the configured horizon
while keeping every power tier (building, suite, row) within its configured
headroom at every month.

Decision variables
------------------
    y[p, t] in {0,1}  -> position p is swapped to its target rack in month t.

A position p is a *candidate* iff its current rack is a retirement candidate
(generation <= optimizer.retire_generation_at_or_below). Each candidate maps to
exactly one target rack type (same family, flagged is_replacement_target).

Constraints
-----------
  * Each candidate is swapped at most once across the horizon.
  * Throughput:   sum_p y[p,t] <= max_swaps_per_month                  (per month)
  * Suite crews:  sum_{p in s} y[p,t] <= max_swaps_per_suite_per_month (per suite/month)
  * Headroom:     for every tier and every month t, cumulative load after all
                  swaps in months <= t must be <= capacity * (1 - headroom_pct).

Objective (minimize)
--------------------
    swap_cost * (#swaps)
  + deferral_penalty * (month each swap lands)        # prefer sooner
  + BIG_M * (#candidates never swapped)               # prefer completing

All weights, limits, capacities and headroom come from Settings — nothing here
is hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.linear_solver import pywraplp

from config.settings import Settings
from floorcast.models.optimization import (
    MonthlyPlan,
    OptimizationResult,
    RunStatus,
    ScheduledSwap,
)
from floorcast.models.rack import Rack


@dataclass
class TierCapacities:
    """Capacity (kW) for each tier id, keyed by tier."""

    building_kw: dict[str, float] = field(default_factory=dict)
    suite_kw: dict[str, float] = field(default_factory=dict)
    row_kw: dict[str, float] = field(default_factory=dict)


@dataclass
class FleetInput:
    """Everything the optimizer needs about the current floor state."""

    racks: list[Rack]
    capacities: TierCapacities


_STATUS_MAP = {
    pywraplp.Solver.OPTIMAL: RunStatus.OPTIMAL,
    pywraplp.Solver.FEASIBLE: RunStatus.FEASIBLE,
    pywraplp.Solver.INFEASIBLE: RunStatus.INFEASIBLE,
}


class RackReplacementOptimizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.opt = settings.optimizer
        self.power = settings.power

    # --------------------------------------------------------------------- #
    def _target_type_for(self, family: str) -> str | None:
        """The single replacement target rack type for a given family."""
        for name, spec in self.settings.replacement_targets().items():
            if spec.family == family:
                return name
        return None

    # --------------------------------------------------------------------- #
    def solve(self, fleet: FleetInput, run_id: str, created_at) -> OptimizationResult:
        s = self.settings
        H = self.opt.horizon_months
        months = range(1, H + 1)
        catalog = s.rack_catalog

        # ---- identify candidates and their target types ----
        candidates: list[tuple[Rack, str, float, float]] = []  # rack, target_type, from_kw, to_kw
        for rack in fleet.racks:
            if not s.is_retirement_candidate(rack.rack_type):
                continue
            target = self._target_type_for(rack.family)
            if target is None:
                continue
            candidates.append(
                (rack, target, rack.power_draw_kw, catalog[target].power_draw_kw)
            )

        solver = pywraplp.Solver.CreateSolver("CBC")
        if solver is None:  # pragma: no cover
            raise RuntimeError("OR-Tools CBC backend unavailable")
        solver.SetTimeLimit(self.opt.solver_time_limit_seconds * 1000)

        # ---- decision vars ----
        y: dict[tuple[int, int], pywraplp.Variable] = {}
        for i, _ in enumerate(candidates):
            for t in months:
                y[(i, t)] = solver.BoolVar(f"y_{i}_{t}")

        # ---- at most one swap per candidate ----
        for i, _ in enumerate(candidates):
            solver.Add(solver.Sum(y[(i, t)] for t in months) <= 1)

        # ---- throughput per month ----
        for t in months:
            solver.Add(
                solver.Sum(y[(i, t)] for i in range(len(candidates)))
                <= self.opt.max_swaps_per_month
            )

        # ---- per-suite throughput per month ----
        suites = {r.suite_id for r, *_ in candidates}
        for sid in suites:
            idxs = [i for i, (r, *_) in enumerate(candidates) if r.suite_id == sid]
            for t in months:
                solver.Add(
                    solver.Sum(y[(i, t)] for i in idxs)
                    <= self.opt.max_swaps_per_suite_per_month
                )

        # ---- headroom per tier per month ----
        # Building and suite headroom are HARD constraints. Row headroom is SOFT:
        # the current floor can already contain a row over its row threshold (a
        # hard row constraint would then be infeasible from month 0, before any
        # decision is made). Instead we allow overage and penalize it.
        self._add_headroom_constraints(solver, y, candidates, fleet, months, "building")
        self._add_headroom_constraints(solver, y, candidates, fleet, months, "suite")
        row_overage = self._add_soft_row_headroom(solver, y, candidates, fleet, months)

        # ---- objective ----
        # Three weighted components, in descending priority by magnitude:
        #   1. completion reward  (big_m = 1e6 / swap)  -> do every feasible swap
        #   2. row overage penalty (config, ~1e3 / kW-month) -> keep rows cool
        #   3. swap cost + deferral (<= ~2.8 / swap) -> fewer, earlier swaps
        # See config/floorcast.yaml::optimizer.row_overage_penalty for the
        # rationale on why (2) sits between (1) and (3).
        big_m = 1_000_000.0
        terms = []
        for i, _ in enumerate(candidates):
            done_i = solver.Sum(y[(i, t)] for t in months)
            # reward completing: subtract big_m * done  (== penalize not done)
            terms.append(-big_m * done_i)
            for t in months:
                terms.append(
                    (self.opt.swap_cost_per_rack + self.opt.deferral_penalty_per_month * t)
                    * y[(i, t)]
                )
        if row_overage:
            terms.append(self.opt.row_overage_penalty * solver.Sum(row_overage.values()))
        solver.Minimize(solver.Sum(terms))

        status = solver.Solve()

        return self._build_result(
            status, solver, y, candidates, fleet, run_id, created_at, H, row_overage
        )

    # --------------------------------------------------------------------- #
    def _tier_key(self, rack: Rack, tier: str) -> str:
        return {
            "building": rack.building_id,
            "suite": rack.suite_id,
            "row": rack.row_id,
        }[tier]

    def _tier_capacity(self, fleet: FleetInput, tier: str, tier_id: str) -> float:
        if tier == "building":
            return fleet.capacities.building_kw[tier_id]
        if tier == "suite":
            return fleet.capacities.suite_kw[tier_id]
        return fleet.capacities.row_kw[tier_id]

    def _add_headroom_constraints(self, solver, y, candidates, fleet, months, tier):
        # base load per tier = sum of CURRENT power for every rack on the floor
        base: dict[str, float] = {}
        for rack in fleet.racks:
            key = self._tier_key(rack, tier)
            base[key] = base.get(key, 0.0) + rack.power_draw_kw

        # candidates grouped by tier, with their power deltas
        by_tier: dict[str, list[tuple[int, float]]] = {}
        for i, (rack, _t, from_kw, to_kw) in enumerate(candidates):
            key = self._tier_key(rack, tier)
            by_tier.setdefault(key, []).append((i, to_kw - from_kw))

        for key, base_load in base.items():
            capacity = self._tier_capacity(fleet, tier, key)
            usable = self.power.usable(capacity)
            members = by_tier.get(key, [])
            for t in months:
                # cumulative: a swap in month u<=t has taken effect by month t
                cumulative = solver.Sum(
                    delta * solver.Sum(y[(i, u)] for u in range(1, t + 1))
                    for (i, delta) in members
                )
                solver.Add(base_load + cumulative <= usable)

    def _add_soft_row_headroom(self, solver, y, candidates, fleet, months):
        """Soft row-tier headroom.

        For each row and month introduce a continuous overage variable
            o[row, t] >= 0
            o[row, t] >= load[row, t] - usable[row]
        where load is base load plus the cumulative power delta of swaps applied
        by month t. The objective penalizes Σ o, so the solver drives every row's
        overage to the minimum its (power-increasing) swaps allow, but the model
        stays feasible even when a row is already over threshold at month 0.

        Returns {(row_id, month): overage_var} for reporting in _build_result.
        """
        base: dict[str, float] = {}
        for rack in fleet.racks:
            base[rack.row_id] = base.get(rack.row_id, 0.0) + rack.power_draw_kw

        by_row: dict[str, list[tuple[int, float]]] = {}
        for i, (rack, _t, from_kw, to_kw) in enumerate(candidates):
            by_row.setdefault(rack.row_id, []).append((i, to_kw - from_kw))

        overage: dict[tuple[str, int], pywraplp.Variable] = {}
        for row_id, base_load in base.items():
            usable = self.power.usable(fleet.capacities.row_kw[row_id])
            members = by_row.get(row_id, [])
            for t in months:
                cumulative = solver.Sum(
                    delta * solver.Sum(y[(i, u)] for u in range(1, t + 1))
                    for (i, delta) in members
                )
                o = solver.NumVar(0.0, solver.infinity(), f"row_over_{row_id}_{t}")
                solver.Add(o >= base_load + cumulative - usable)
                overage[(row_id, t)] = o
        return overage

    # --------------------------------------------------------------------- #
    def _build_result(
        self, status, solver, y, candidates, fleet, run_id, created_at, H, row_overage
    ) -> OptimizationResult:
        run_status = _STATUS_MAP.get(status, RunStatus.FAILED)
        if run_status in (RunStatus.INFEASIBLE, RunStatus.FAILED):
            return OptimizationResult(
                run_id=run_id,
                status=run_status,
                horizon_months=H,
                total_swaps=0,
                plan=[],
                total_row_overage_kw=0.0,
                solver_wall_time_ms=int(solver.WallTime()),
                created_at=created_at,
            )

        plans: dict[int, MonthlyPlan] = {t: MonthlyPlan(month=t) for t in range(1, H + 1)}
        total = 0
        for i, (rack, target, from_kw, to_kw) in enumerate(candidates):
            for t in range(1, H + 1):
                if y[(i, t)].solution_value() > 0.5:
                    plans[t].swaps.append(
                        ScheduledSwap(
                            position_id=rack.position_id,
                            suite_id=rack.suite_id,
                            building_id=rack.building_id,
                            from_rack_type=rack.rack_type,
                            to_rack_type=target,
                            from_power_kw=from_kw,
                            to_power_kw=to_kw,
                            month=t,
                        )
                    )
                    total += 1
                    break

        self._annotate_utilization(plans, candidates, fleet, H)

        # Row overage (soft tier): read the solved overage vars per month + total.
        month_overage: dict[int, float] = {t: 0.0 for t in range(1, H + 1)}
        for (row_id, t), var in row_overage.items():
            month_overage[t] += max(0.0, var.solution_value())
        total_overage = 0.0
        for t in range(1, H + 1):
            plans[t].row_overage_kw = round(month_overage[t], 4)
            total_overage += month_overage[t]

        return OptimizationResult(
            run_id=run_id,
            status=run_status,
            objective_value=solver.Objective().Value(),
            horizon_months=H,
            total_swaps=total,
            plan=[plans[t] for t in range(1, H + 1)],
            total_row_overage_kw=round(total_overage, 4),
            solver_wall_time_ms=int(solver.WallTime()),
            created_at=created_at,
        )

    def _annotate_utilization(self, plans, candidates, fleet, H):
        """Fill per-month peak utilization per building/suite for charting."""
        # cumulative deltas applied by month, keyed by tier id
        swap_by_month = {t: plans[t].swaps for t in range(1, H + 1)}

        def base_for(tier: str) -> dict[str, float]:
            b: dict[str, float] = {}
            for rack in fleet.racks:
                key = self._tier_key(rack, tier)
                b[key] = b.get(key, 0.0) + rack.power_draw_kw
            return b

        b_load, s_load = base_for("building"), base_for("suite")
        for t in range(1, H + 1):
            for swap in swap_by_month[t]:
                delta = swap.to_power_kw - swap.from_power_kw
                b_load[swap.building_id] += delta
                s_load[swap.suite_id] += delta
            plans[t].building_peak_util = {
                k: round(v / fleet.capacities.building_kw[k], 4) for k, v in b_load.items()
            }
            plans[t].suite_peak_util = {
                k: round(v / fleet.capacities.suite_kw[k], 4) for k, v in s_load.items()
            }
