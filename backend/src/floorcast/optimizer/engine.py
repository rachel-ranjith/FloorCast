"""ILP rack-replacement optimizer (OR-Tools / CBC MIP).

Produces a month-by-month rack replacement schedule over the configured horizon
while keeping every power tier (building, suite, row) within its configured
headroom at every month.

Decision variables
------------------
    y[i, g, t] in {0,1}  -> candidate i is swapped to target g in month t.

A position is a *candidate* iff its current rack is a retirement candidate
(generation <= optimizer.retire_generation_at_or_below). Each candidate may be
swapped to ANY of its valid targets g (a catalog entry whose family matches and
is flagged is_replacement_target). The solver CHOOSES the target — `g` is a
decision dimension, not resolved up front. When a family has exactly one valid
target, y[i, g, t] collapses to the historical y[i, t] behaviour.

Keeping `g` as an explicit index (rather than a "which target" integer variable)
keeps every power delta a CONSTANT coefficient — the model stays fully linear,
with no variable*variable products and no big-M linearization for power.

Constraints
-----------
  * Each candidate is swapped at most once: sum_{g,t} y[i,g,t] <= 1.
    This single constraint also enforces single-target-choice (at most one
    (g, t) pair can be 1, so at most one target is selected).
  * Throughput:   sum_{i,g} y[i,g,t] <= max_swaps_per_month                  (per month)
  * Suite crews:  sum_{i in s,g} y[i,g,t] <= max_swaps_per_suite_per_month   (per suite/month)
  * Headroom:     for every tier and every month t, cumulative load after all
                  swaps in months <= t must be <= capacity * (1 - headroom_pct).
                  The per-swap power delta now sums over g: each target's power
                  is a constant, so the term stays linear.
  * Budget (optional): sum_{i,g,t} cost_g * y[i,g,t] <= budget_cap.

Objective
---------
Selectable via optimizer.objective (consistent maximize_/minimize_ prefixes).
Implemented:
  * maximize_racks_modernized   -> historical behaviour (completion reward, then
    swap cost + deferral, then row-overage penalty).
  * minimize_headroom_violation -> minimize total row-tier overage, earliest
    months weighted most (see _obj_minimize_headroom_violation for the honest
    caveat that swaps only ADD power).
  * maximize_value_per_pound    -> per-swap value = target.compute / target.cost.
  * maximize_compute            -> per-swap value = target.compute - retiring.compute.
  * maximize_efficiency         -> per-swap value = target.compute / target.power.
    (The three value objectives share _obj_maximize_value; each routes its value
    through _time_weight and keeps the model linear.)
All recognized objectives are implemented; an unknown name raises ValueError.

Time-weighting (Stage 4): a shared, objective-agnostic weight w(t) =
optimizer.time_discount^(t-1) multiplies whatever value an objective delivers in
month t, so earlier delivery scores higher. time_discount defaults to 1.0
(w(t)==1, fully inert), preserving historical results exactly.

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
from floorcast.optimizer.calendar import (
    PlanMonth,
    abstract_calendar,
    calendar_from_range,
)


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

# Recognised objectives (all implemented): maximize_racks_modernized,
# minimize_headroom_violation, maximize_value_per_pound, maximize_compute,
# maximize_efficiency. The dispatch in _build_objective handles each by name and
# raises ValueError for anything else.


class RackReplacementOptimizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.opt = settings.optimizer
        self.power = settings.power

    # --------------------------------------------------------------------- #
    def _targets_for(self, family: str) -> list[str]:
        """All valid replacement target type names for a family.

        A valid target = a catalog entry whose family matches AND is flagged
        is_replacement_target. May return MULTIPLE targets — the solver chooses.
        """
        return [
            name
            for name, spec in self.settings.replacement_targets().items()
            if spec.family == family
        ]

    # --------------------------------------------------------------------- #
    def _build_calendar(self) -> list[PlanMonth]:
        """Plan steps as calendar months (if a date range is set) or abstract.

        With no active date range this returns abstract_calendar(horizon_months),
        which is identical to pre-Stage-3 behaviour (steps 1..N, no quarters).
        """
        cal = self.opt.calendar
        if cal.is_active:
            return calendar_from_range(
                cal.start_year, cal.start_month, cal.end_year, cal.end_month
            )
        return abstract_calendar(self.opt.horizon_months)

    def solve(self, fleet: FleetInput, run_id: str, created_at) -> OptimizationResult:
        s = self.settings
        calendar = self._build_calendar()
        H = len(calendar)
        months = range(1, H + 1)
        catalog = s.rack_catalog

        # ---- candidates (just the retiring racks) + their valid targets ----
        candidates: list[Rack] = []
        cand_targets: list[list[str]] = []
        for rack in fleet.racks:
            if not s.is_retirement_candidate(rack.rack_type):
                continue
            targets = self._targets_for(rack.family)
            if not targets:
                continue
            candidates.append(rack)
            cand_targets.append(targets)

        solver = pywraplp.Solver.CreateSolver("CBC")
        if solver is None:  # pragma: no cover
            raise RuntimeError("OR-Tools CBC backend unavailable")
        solver.SetTimeLimit(self.opt.solver_time_limit_seconds * 1000)

        # ---- decision vars: y[i, g, t] ----
        y: dict[tuple[int, str, int], pywraplp.Variable] = {}
        for i, _rack in enumerate(candidates):
            for g in cand_targets[i]:
                for t in months:
                    y[(i, g, t)] = solver.BoolVar(f"y_{i}_{g}_{t}")

        # ---- at most one swap (and thus one target) per candidate ----
        for i, _rack in enumerate(candidates):
            solver.Add(
                solver.Sum(y[(i, g, t)] for g in cand_targets[i] for t in months) <= 1
            )

        # ---- throughput per month ----
        for t in months:
            solver.Add(
                solver.Sum(
                    y[(i, g, t)]
                    for i in range(len(candidates))
                    for g in cand_targets[i]
                )
                <= self.opt.max_swaps_per_month
            )

        # ---- per-suite throughput per month ----
        suites = {r.suite_id for r in candidates}
        for sid in suites:
            idxs = [i for i, r in enumerate(candidates) if r.suite_id == sid]
            for t in months:
                solver.Add(
                    solver.Sum(
                        y[(i, g, t)] for i in idxs for g in cand_targets[i]
                    )
                    <= self.opt.max_swaps_per_suite_per_month
                )

        # ---- headroom per tier per month ----
        # Building and suite headroom are HARD constraints. Row headroom is SOFT:
        # the current floor can already contain a row over its row threshold (a
        # hard row constraint would then be infeasible from month 0, before any
        # decision is made). Instead we allow overage and penalize it.
        self._add_headroom_constraints(
            solver, y, candidates, cand_targets, catalog, fleet, months, "building"
        )
        self._add_headroom_constraints(
            solver, y, candidates, cand_targets, catalog, fleet, months, "suite"
        )
        row_overage = self._add_soft_row_headroom(
            solver, y, candidates, cand_targets, catalog, fleet, months
        )

        # ---- optional budget caps (stackable: total / year / quarter / month) ----
        self._add_budget_constraints(solver, y, candidates, cand_targets, catalog, calendar)

        # ---- optional per-target count constraints (at-least / at-most) ----
        self._add_count_constraints(solver, y, candidates, cand_targets, months)

        # ---- objective (selectable) ----
        self._build_objective(
            solver, y, candidates, cand_targets, catalog, months, row_overage
        )

        status = solver.Solve()

        return self._build_result(
            status, solver, y, candidates, cand_targets, catalog,
            fleet, run_id, created_at, calendar, row_overage,
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

    def _cumulative_delta(self, solver, y, members, cand_targets, catalog, t):
        """Cumulative power delta on a tier by month t, summed over targets.

        members: list of (candidate_index, current_power_kw) on this tier key.
        For each candidate, a swap to target g changes power by
        (catalog[g].power_draw_kw - from_kw); each target's power is a CONSTANT,
        so the whole expression is linear in the y variables.
        """
        return solver.Sum(
            (catalog[g].power_draw_kw - from_kw) * y[(i, g, u)]
            for (i, from_kw) in members
            for g in cand_targets[i]
            for u in range(1, t + 1)
        )

    def _add_headroom_constraints(
        self, solver, y, candidates, cand_targets, catalog, fleet, months, tier
    ):
        # base load per tier = sum of CURRENT power for every rack on the floor
        base: dict[str, float] = {}
        for rack in fleet.racks:
            key = self._tier_key(rack, tier)
            base[key] = base.get(key, 0.0) + rack.power_draw_kw

        # candidates grouped by tier, with their current power (delta resolves
        # per chosen target g inside _cumulative_delta).
        by_tier: dict[str, list[tuple[int, float]]] = {}
        for i, rack in enumerate(candidates):
            key = self._tier_key(rack, tier)
            by_tier.setdefault(key, []).append((i, rack.power_draw_kw))

        for key, base_load in base.items():
            capacity = self._tier_capacity(fleet, tier, key)
            usable = self.power.usable(capacity)
            members = by_tier.get(key, [])
            for t in months:
                cumulative = self._cumulative_delta(
                    solver, y, members, cand_targets, catalog, t
                )
                solver.Add(base_load + cumulative <= usable)

    def _add_soft_row_headroom(
        self, solver, y, candidates, cand_targets, catalog, fleet, months
    ):
        """Soft row-tier headroom.

        For each row and month introduce a continuous overage variable
            o[row, t] >= 0
            o[row, t] >= load[row, t] - usable[row]
        where load is base load plus the cumulative power delta of swaps applied
        by month t (summed over chosen targets). The objective penalizes Σ o, so
        the solver drives every row's overage to the minimum its swaps allow, but
        the model stays feasible even when a row is already over threshold at
        month 0.

        Returns {(row_id, month): overage_var} for reporting in _build_result.
        """
        base: dict[str, float] = {}
        for rack in fleet.racks:
            base[rack.row_id] = base.get(rack.row_id, 0.0) + rack.power_draw_kw

        by_row: dict[str, list[tuple[int, float]]] = {}
        for i, rack in enumerate(candidates):
            by_row.setdefault(rack.row_id, []).append((i, rack.power_draw_kw))

        overage: dict[tuple[str, int], pywraplp.Variable] = {}
        for row_id, base_load in base.items():
            usable = self.power.usable(fleet.capacities.row_kw[row_id])
            members = by_row.get(row_id, [])
            for t in months:
                cumulative = self._cumulative_delta(
                    solver, y, members, cand_targets, catalog, t
                )
                o = solver.NumVar(0.0, solver.infinity(), f"row_over_{row_id}_{t}")
                solver.Add(o >= base_load + cumulative - usable)
                overage[(row_id, t)] = o
        return overage

    @staticmethod
    def _budget_groups(period: str, calendar) -> list[list[int]]:
        """Group plan steps for a budget period -> list of month-step groups.

          total   -> one group with every step                  (no calendar needed)
          month   -> one group per step                          (no calendar needed)
          quarter -> one group per fiscal quarter (pm.quarter)   (requires calendar)
          year    -> one group per calendar year (pm.year)       (requires calendar)

        quarter/year are calendar-only: in abstract-month mode pm.quarter / pm.year
        are None, so a clean ValueError is raised (same guard as the old per-quarter
        builder). Partial quarters/years fall out naturally — a group holds only the
        in-range months.
        """
        if period == "total":
            return [[pm.step for pm in calendar]]
        if period == "month":
            return [[pm.step] for pm in calendar]
        key = {"quarter": lambda pm: pm.quarter, "year": lambda pm: pm.year}[period]
        if not any(key(pm) is not None for pm in calendar):
            raise ValueError(
                f"a budget cap with period={period!r} requires an active date range "
                f"(optimizer.calendar): {period}s only exist in calendar mode"
            )
        groups: dict = {}
        for pm in calendar:
            groups.setdefault(key(pm), []).append(pm.step)
        return list(groups.values())

    def _add_budget_constraints(self, solver, y, candidates, cand_targets, catalog, calendar):
        """Unified, stackable capital-budget caps: "no more than £cap per period".

        For each rule (from optimizer.budget_caps plus the back-compat scalar
        fields, via OptimizerConfig.all_budget_caps()), group the plan's months by
        the rule's period and, per group, add:
            Σ_{i,g, t in group} cost_g * y[i,g,t] <= cap.
        cost_g is a constant per (i, g), so each constraint is LINEAR (a sum of
        binaries with constant coefficients, bounded by a constant). No rule alone
        can make the model infeasible: zero swaps in any group satisfies <= cap, so
        a too-tight cap simply yields fewer swaps. Empty rule list => no-op.
        """
        for rule in self.opt.all_budget_caps():
            for steps in self._budget_groups(rule.period, calendar):
                spend = solver.Sum(
                    catalog[g].cost * y[(i, g, t)]
                    for i in range(len(candidates))
                    for g in cand_targets[i]
                    for t in steps
                )
                solver.Add(spend <= rule.cap)

    def _add_count_constraints(self, solver, y, candidates, cand_targets, months):
        """Optional per-target "at least N" / "at most N" install-count bounds.

        For each constrained target type g_target:
            count_g = Σ_{i can take g_target, t} y[i, g_target, t]   (# installs)
            min -> solver.Add(count_g >= min)
            max -> solver.Add(count_g <= max)
        count_g is a sum of binaries and the bounds are constants, so these are
        LINEAR. They compose with every other constraint (objective, budget,
        per-quarter, calendar, headroom) — same y[i,g,t] variables.

        Behaviour:
          * a `min` may be UNSATISFIABLE (fewer candidates can take g_target than
            min, or it collides with budget/throughput/headroom) -> the model is
            genuinely INFEASIBLE (CBC reports it; _build_result returns an empty
            plan, no crash). If no candidate can take g_target at all, count_g is
            the constant 0, so min>0 is infeasible and max>=0 is trivially met.
          * a `max` alone NEVER causes infeasibility — the solver can always
            install fewer (down to zero) of that type.

        Validation (clear ValueError before solving):
          * a type that isn't a valid replacement target (not in the catalog or
            not is_replacement_target) — nothing can swap to it.
          * min > max on the same type.
        """
        constraints = self.opt.count_constraints
        if not constraints:
            return
        valid_targets = set(self.settings.replacement_targets().keys())
        for g_target, bound in constraints.items():
            if g_target not in valid_targets:
                raise ValueError(
                    f"count_constraints names {g_target!r}, which is not a valid "
                    f"replacement target (must be a catalog entry with "
                    f"is_replacement_target: true)"
                )
            lo, hi = bound.min, bound.max
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(
                    f"count_constraints[{g_target!r}]: min ({lo}) > max ({hi})"
                )
            count_g = solver.Sum(
                y[(i, g_target, t)]
                for i in range(len(candidates))
                if g_target in cand_targets[i]
                for t in months
            )
            if lo is not None:
                solver.Add(count_g >= lo)
            if hi is not None:
                solver.Add(count_g <= hi)

    # --------------------------------------------------------------------- #
    # Time-weighting (shared by all objectives)
    # --------------------------------------------------------------------- #
    def _time_weight(self, t: int) -> float:
        """Objective-agnostic time weight for a value delivered in month t.

        w(t) = time_discount^(t-1). `t` is a fixed loop index and time_discount
        is a config constant, so w(t) is a plain NUMBER — multiplying it by a
        binary y (or a continuous overage var) keeps the model LINEAR (this is
        NOT an exponential of a decision variable).

        At the default time_discount = 1.0, w(t) == 1.0 for every month, so the
        mechanism is INERT and any objective that routes value through it reduces
        exactly to its un-timed form. Below 1.0, earlier months weigh more, so
        the solver prefers delivering objective value sooner.
        """
        return self.opt.time_discount ** (t - 1)

    # --------------------------------------------------------------------- #
    # Objective dispatch
    # --------------------------------------------------------------------- #
    def _build_objective(self, solver, y, candidates, cand_targets, catalog, months, row_overage):
        obj = self.opt.objective
        if obj == "maximize_racks_modernized":
            self._obj_maximize_racks_modernized(
                solver, y, candidates, cand_targets, months, row_overage
            )
            return
        if obj == "minimize_headroom_violation":
            self._obj_minimize_headroom_violation(
                solver, y, candidates, cand_targets, months, row_overage
            )
            return
        # ---- Stage 5: per-swap value objectives (share _time_weight) ----
        # Each value_fn(rack, target_spec) returns a CONSTANT per (candidate i,
        # target g): a ratio/difference of known catalog + retiring-rack numbers,
        # computed before the solve. So value * w(t) * y stays linear (no
        # variable in a denominator).
        def _old_compute(rack) -> float:
            spec = catalog.get(rack.rack_type)
            return spec.compute_capability if spec is not None else 0.0

        if obj == "maximize_value_per_pound":
            # capability-per-pound of the INSTALLED target (see _obj_maximize_value
            # docstring for why this is target capability/cost, not gain/cost).
            self._obj_maximize_value(
                solver, y, candidates, cand_targets, catalog, months, row_overage,
                lambda rack, tgt: tgt.compute_capability / tgt.cost,
            )
            return
        if obj == "maximize_compute":
            # raw capability GAINED by the swap (new minus retiring), ignoring cost.
            self._obj_maximize_value(
                solver, y, candidates, cand_targets, catalog, months, row_overage,
                lambda rack, tgt: tgt.compute_capability - _old_compute(rack),
            )
            return
        if obj == "maximize_efficiency":
            # capability per kW of the chosen target (compute density).
            self._obj_maximize_value(
                solver, y, candidates, cand_targets, catalog, months, row_overage,
                lambda rack, tgt: tgt.compute_capability / tgt.power_draw_kw,
            )
            return
        raise ValueError(f"unknown optimizer.objective: {obj!r}")

    def _obj_maximize_racks_modernized(
        self, solver, y, candidates, cand_targets, months, row_overage
    ):
        """Maximize racks modernized (historical default behaviour).

        Expressed as a minimization with three weighted components, in descending
        priority by magnitude:
          1. completion reward  (big_m = 1e6 / swap)  -> do every feasible swap
          2. row overage penalty (config, ~1e3 / kW-month) -> keep rows cool
          3. swap cost + deferral (<= ~2.8 / swap) -> fewer, earlier swaps
        See config/floorcast.yaml::optimizer.row_overage_penalty for why (2) sits
        between (1) and (3).

        Each swap's "value" is 1 (one rack modernized), routed through the shared
        time-weight: the completion reward is big_m * w(t) per swap. At the
        default time_discount = 1.0, w(t) == 1, so this is byte-for-byte the
        pre-Stage-4 objective (-big_m * done_i). With discounting on, the reward
        decays with t, front-loading modernization.
        """
        big_m = 1_000_000.0
        terms = []
        for i, _rack in enumerate(candidates):
            for g in cand_targets[i]:
                for t in months:
                    # value = 1 rack; reward (negative => minimized) is time-weighted
                    terms.append(-big_m * self._time_weight(t) * y[(i, g, t)])
                    terms.append(
                        (
                            self.opt.swap_cost_per_rack
                            + self.opt.deferral_penalty_per_month * t
                        )
                        * y[(i, g, t)]
                    )
        if row_overage:
            terms.append(
                self.opt.row_overage_penalty * solver.Sum(row_overage.values())
            )
        solver.Minimize(solver.Sum(terms))

    def _obj_minimize_headroom_violation(
        self, solver, y, candidates, cand_targets, months, row_overage
    ):
        """Minimize headroom violation, with relief delivered as early as possible.

        HONEST CAVEAT: every swap replaces a retiring rack with a higher-power one,
        so a swap can only ADD load — no swap ever *reduces* a row's draw. There is
        therefore no genuine per-swap "relief" value to maximize. The thing the
        optimizer can actually control about headroom is the overage it CREATES:
        which swaps it does, which (possibly lower-power) target it picks, and when.
        So "minimize headroom violation" is implemented as: minimize the total
        row-tier overage across the plan, time-weighted so EARLY overage is the
        most expensive to incur (preserve headroom sooner) -- i.e. relief = "keep
        the floor within headroom, earliest months first".

        Weighting (descending magnitude), as a minimization:
          1. overage  (1e6 * w(t) / kW-month) -> crush overage; earlier counts more
          2. modernization reward (100 * w(t) / swap) -> still modernize, but only
             where it does not push a row over (overage dominates this by 1e4x, so
             an overage-causing swap is skipped/deferred rather than performed)
          3. swap cost + deferral (~3 / swap) -> tie-breakers
        This deliberately INVERTS the priority of maximize_racks_modernized (where
        completion dominates overage), so on a floor where some swaps create overage
        the two objectives produce visibly different plans.
        """
        overage_weight = 1_000_000.0
        mod_reward = 100.0  # >> swap_cost + deferral, << overage_weight
        terms = []
        # 1. time-weighted total overage (primary).
        for (row_id, t), o in row_overage.items():
            terms.append(overage_weight * self._time_weight(t) * o)
        # 2. modernization reward + 3. tie-breakers (secondary/tertiary).
        for i, _rack in enumerate(candidates):
            for g in cand_targets[i]:
                for t in months:
                    terms.append(-mod_reward * self._time_weight(t) * y[(i, g, t)])
                    terms.append(
                        (
                            self.opt.swap_cost_per_rack
                            + self.opt.deferral_penalty_per_month * t
                        )
                        * y[(i, g, t)]
                    )
        solver.Minimize(solver.Sum(terms))

    def _obj_maximize_value(
        self, solver, y, candidates, cand_targets, catalog, months, row_overage, value_fn
    ):
        """Generic value-maximizing objective shared by the Stage-5 objectives.

        value_fn(rack, target_spec) -> per-swap value for choosing target g for
        candidate i. It is a CONSTANT (computed from catalog + the retiring rack
        before the solve), so the model stays linear: maximizing the time-weighted
        value is `minimize  -V_norm * v(i,g) * w(t) * y[i,g,t]`.

        Definitions used by the three callers:
          * maximize_value_per_pound : target.compute / target.cost. We use the
            installed target's capability-per-pound, NOT (gain / cost). gain/cost
            would always favour the largest-absolute-gain rack (the power-hungry
            "-max"), collapsing this objective into maximize_compute; capability/
            cost is the catalog's true bang-for-buck and is what makes the "-value"
            variant win, per the demo target table.
          * maximize_compute        : target.compute - retiring.compute (raw gain).
          * maximize_efficiency      : target.compute / target.power (compute/kW).

        Values are NORMALIZED by their max magnitude so the primary tier dominates
        regardless of the metric's natural scale (compute gain ~10^2, value/£
        ~10^-3, compute/kW ~10^1). Normalization is a single positive scalar, so
        the argmax — and thus every target choice — is unchanged.

        Magnitude tiers (minimization), mirroring maximize_racks_modernized:
          1. value reward    : <= 1e6 * w(t) per swap (normalized) -> pick the
             highest-value candidates AND targets, earlier first; fills the
             throughput quota since every value is >= 0 for a real upgrade.
          2. row overage     : row_overage_penalty (~1e3 / kW-month) -> respected
             but LOWER priority than value (these objectives accept overage to buy
             value, the deliberate contrast with minimize_headroom_violation).
          3. swap cost + deferral (~3 / swap) -> tie-breakers.
        """
        values: dict[tuple[int, str], float] = {}
        for i, rack in enumerate(candidates):
            for g in cand_targets[i]:
                values[(i, g)] = value_fn(rack, catalog[g])
        v_max = max((abs(v) for v in values.values()), default=1.0) or 1.0

        big_value = 1_000_000.0
        terms = []
        for i, _rack in enumerate(candidates):
            for g in cand_targets[i]:
                v = values[(i, g)] / v_max  # normalized; positive scalar preserves argmax
                for t in months:
                    terms.append(-big_value * v * self._time_weight(t) * y[(i, g, t)])
                    terms.append(
                        (
                            self.opt.swap_cost_per_rack
                            + self.opt.deferral_penalty_per_month * t
                        )
                        * y[(i, g, t)]
                    )
        if row_overage:
            terms.append(
                self.opt.row_overage_penalty * solver.Sum(row_overage.values())
            )
        solver.Minimize(solver.Sum(terms))

    # --------------------------------------------------------------------- #
    def _build_result(
        self, status, solver, y, candidates, cand_targets, catalog,
        fleet, run_id, created_at, calendar, row_overage,
    ) -> OptimizationResult:
        H = len(calendar)
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

        # One plan per step, pre-labelled from the calendar.
        plans: dict[int, MonthlyPlan] = {
            pm.step: MonthlyPlan(
                month=pm.step,
                label=pm.label,
                year=pm.year,
                calendar_month=pm.month,
                quarter=pm.quarter,
            )
            for pm in calendar
        }
        step_meta = {pm.step: pm for pm in calendar}

        total = 0
        for i, rack in enumerate(candidates):
            chosen = self._chosen_swap(y, i, cand_targets[i], H)
            if chosen is None:
                continue
            g, t = chosen
            to_kw = catalog[g].power_draw_kw
            plans[t].swaps.append(
                ScheduledSwap(
                    position_id=rack.position_id,
                    row_id=rack.row_id,
                    suite_id=rack.suite_id,
                    building_id=rack.building_id,
                    from_rack_type=rack.rack_type,
                    to_rack_type=g,
                    from_power_kw=rack.power_draw_kw,
                    to_power_kw=to_kw,
                    month=t,
                )
            )
            total += 1

        self._annotate_utilization(plans, fleet, H)

        # ---- per-period spend (£) from the chosen swaps ----
        monthly_spend: dict[str, float] = {pm.label: 0.0 for pm in calendar}
        quarterly_spend: dict[str, float] = {}
        yearly_spend: dict[str, float] = {}
        for t in range(1, H + 1):
            pm = step_meta[t]
            month_cost = sum(catalog[sw.to_rack_type].cost for sw in plans[t].swaps)
            plans[t].spend = round(month_cost, 4)
            monthly_spend[pm.label] = round(month_cost, 4)
            if pm.quarter is not None:
                quarterly_spend[pm.quarter] = round(
                    quarterly_spend.get(pm.quarter, 0.0) + month_cost, 4
                )
            if pm.year is not None:
                yearly_spend[str(pm.year)] = round(
                    yearly_spend.get(str(pm.year), 0.0) + month_cost, 4
                )

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
            monthly_spend=monthly_spend,
            quarterly_spend=quarterly_spend,
            yearly_spend=yearly_spend,
            solver_wall_time_ms=int(solver.WallTime()),
            created_at=created_at,
        )

    @staticmethod
    def _chosen_swap(y, i, targets, H):
        """Return the (target, month) the solver picked for candidate i, or None.

        The at-most-one constraint guarantees at most one (g, t) is set, so the
        first match is the unique choice.
        """
        for g in targets:
            for t in range(1, H + 1):
                if y[(i, g, t)].solution_value() > 0.5:
                    return g, t
        return None

    def _annotate_utilization(self, plans, fleet, H):
        """Fill per-month peak utilization per building/suite for charting."""
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
