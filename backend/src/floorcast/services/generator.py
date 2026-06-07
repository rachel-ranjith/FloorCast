"""Deterministic fake-data-centre generator.

Builds a topology (buildings > suites > rows > positions) and populates a subset
of positions with racks drawn from the configurable rack catalog. Used by the
seed script and by tests.

Capacities and rack power draws come entirely from Settings; counts and fill
density are parameters so the same generator can make a tiny test floor or a
realistic production-scale one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import Settings
from floorcast.models.rack import Rack, RackState
from floorcast.models.topology import Building, RackPosition, Row, Suite


@dataclass
class GeneratorConfig:
    buildings: int = 2
    suites_per_building: int = 4
    rows_per_suite: int = 48
    positions_per_row: int = 10
    fill_ratio: float = 0.12  # fraction of positions that hold a rack
    seed: int = 42

    # Relative likelihood of each (family, generation) for seeded racks.
    # Keys must exist in the rack catalog. Only 2023/2024 are seeded as the
    # existing fleet; 2025 types are what the optimizer installs later.
    family_mix: dict[str, float] = field(
        default_factory=lambda: {
            "compute-2023": 0.30,
            "storage-2023": 0.20,
            "ai-2023": 0.05,
            "compute-2024": 0.25,
            "storage-2024": 0.15,
            "ai-2024": 0.05,
        }
    )


@dataclass
class GeneratedFloor:
    buildings: list[Building]
    racks: list[Rack]


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights.keys())
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def generate_floor(settings: Settings, gcfg: GeneratorConfig | None = None) -> GeneratedFloor:
    gcfg = gcfg or GeneratorConfig()
    rng = random.Random(gcfg.seed)
    now = datetime.now(timezone.utc)
    catalog = settings.rack_catalog

    # Validate the mix references real catalog entries up front.
    for rack_type in gcfg.family_mix:
        if rack_type not in catalog:
            raise ValueError(f"family_mix references unknown rack type: {rack_type}")

    building_cap_kw = settings.power.building_capacity_mw * 1000.0
    suite_cap_kw = settings.power.suite_capacity_mw * 1000.0
    row_cap_kw = settings.power.row_capacity_kw

    buildings: list[Building] = []
    racks: list[Rack] = []

    for b in range(1, gcfg.buildings + 1):
        bid = f"b{b}"
        building = Building(building_id=bid, label=f"Building {b}", capacity_mw=settings.power.building_capacity_mw)

        for sx in range(1, gcfg.suites_per_building + 1):
            sid = f"{bid}-s{sx}"
            suite = Suite(
                suite_id=sid,
                building_id=bid,
                ordinal=sx,
                label=f"Suite {sx}",
                capacity_mw=settings.power.suite_capacity_mw,
            )

            for rx in range(1, gcfg.rows_per_suite + 1):
                rid = f"{sid}-r{rx:02d}"
                row = Row(
                    row_id=rid,
                    suite_id=sid,
                    building_id=bid,
                    ordinal=rx,
                    label=f"Row {rx:02d}",
                    capacity_kw=row_cap_kw,
                )

                for px in range(gcfg.positions_per_row):
                    pid = f"{rid}-p{px:02d}"
                    occupied = rng.random() < gcfg.fill_ratio
                    rack_id = None
                    if occupied:
                        rack_type = _weighted_choice(rng, gcfg.family_mix)
                        spec = catalog[rack_type]
                        rack_id = f"rk-{bid}-{sx}-{rx:02d}-{px:02d}"
                        racks.append(
                            Rack(
                                rack_id=rack_id,
                                position_id=pid,
                                row_id=rid,
                                suite_id=sid,
                                building_id=bid,
                                rack_type=rack_type,
                                family=spec.family,
                                generation=spec.generation,
                                power_draw_kw=spec.power_draw_kw,
                                state=RackState.ACTIVE,
                                installed_at=now,
                                updated_at=now,
                            )
                        )
                    row.positions.append(
                        RackPosition(
                            position_id=pid,
                            row_id=rid,
                            suite_id=sid,
                            building_id=bid,
                            ordinal=px,
                            occupied=occupied,
                            rack_id=rack_id,
                        )
                    )
                suite.rows.append(row)
            building.suites.append(suite)
        buildings.append(building)

    # Unused but kept for clarity / future capacity-aware seeding.
    _ = (building_cap_kw, suite_cap_kw)
    return GeneratedFloor(buildings=buildings, racks=racks)


# --------------------------------------------------------------------------- #
# The standard floor — deliberately engineered to make objectives diverge.
# --------------------------------------------------------------------------- #
# Requires the standard catalog (config/floorcast.yaml): multiple replacement
# targets per family + retire_generation_at_or_below: 2024. Unlike generate_floor
# (uniform grid, random fill — kept for tests/utilities) this floor is hand-laid
# by ROW ARCHETYPE so the tension is exact and reproducible. `scripts/seed_data.py`
# seeds it and prints the verification report.
#
# Archetypes (row usable = 72 kW at the default 20% headroom):
#   COOL : 3 small 2023/2024 candidates; safe even if all swap to a max target
#          (<=51 kW) -> the pool of zero-overage "safe" swaps.
#   NEAR : 38.5 kW of immovable gen-2025 ballast + one ai-2023 candidate
#          (base 60.5 kW, OK). ANY 2025 ai target overflows the row (eff +38 ->
#          76.5) -> SWAP-AVOIDABLE overage: doing the swap creates a violation
#          that skipping (or a smaller target) would avoid.
#   STRUCT: gen-2025 ballast that is already over 72 kW with zero swaps and holds
#          no candidate -> UNAVOIDABLE (structural) overage; identical under every
#          objective.
#   EMPTY: no racks (realistic slack / render variety).
_DEMO_COOL_VARIANTS = [
    ["compute-2023", "storage-2023", "compute-2024"],   # 8.5 + 6 + 11  = 25.5
    ["storage-2024", "compute-2023", "storage-2023"],   # 7.5 + 8.5 + 6 = 22.0
    ["compute-2024", "compute-2023", "storage-2024"],   # 11 + 8.5 + 7.5 = 27.0
]
_DEMO_NEAR_FILLER = ["compute-2025-max", "compute-2025-value", "storage-2025-eff"]  # 38.5 kW
_DEMO_NEAR_CANDIDATE = "ai-2023"          # 22 kW now; every ai-2025 target tips the row over
_DEMO_STRUCT_FILLER = ["ai-2025-max", "storage-2025-max"]  # 76 kW > 72 (immovable, no candidate)

# Heterogeneous estate: 3 buildings, 2/4/3 suites, varied rows per suite.
# Each suite tuple is (label, n_cool, n_near, n_struct, n_empty).
_DEMO_LAYOUT = [
    ("bA", "Building A", [
        ("Suite 1", 3, 5, 1, 2),
        ("Suite 2", 2, 6, 0, 1),
    ]),
    ("bB", "Building B", [
        ("Suite 1", 3, 5, 1, 1),
        ("Suite 2", 2, 6, 1, 1),
        ("Suite 3", 3, 4, 0, 2),
        ("Suite 4", 2, 5, 1, 1),
    ]),
    ("bC", "Building C", [
        ("Suite 1", 3, 4, 1, 1),
        ("Suite 2", 2, 5, 0, 2),
        ("Suite 3", 2, 4, 1, 1),
    ]),
]


def generate_demo_floor(
    settings: Settings, positions_per_row: int = 10, seed: int = 42
) -> GeneratedFloor:
    """Build the tension demo floor (see module notes + _DEMO_LAYOUT).

    Fully deterministic by construction (the `seed` argument is accepted for CLI
    symmetry but the layout does not vary). Raises a clear error if the active
    catalog lacks the demo target types (i.e. you forgot the demo config).
    """
    catalog = settings.rack_catalog
    now = datetime.now(timezone.utc)

    needed = (
        set(_DEMO_NEAR_FILLER)
        | set(_DEMO_STRUCT_FILLER)
        | {_DEMO_NEAR_CANDIDATE}
        | {t for variant in _DEMO_COOL_VARIANTS for t in variant}
    )
    missing = sorted(t for t in needed if t not in catalog)
    if missing:
        raise ValueError(
            "demo floor needs catalog types absent from the active config "
            f"{missing}; expected the standard config/floorcast.yaml catalog"
        )

    buildings: list[Building] = []
    racks: list[Rack] = []
    cool_i = 0  # rotates cool variants across the whole floor for family spread

    for bid, blabel, suites in _DEMO_LAYOUT:
        building = Building(
            building_id=bid, label=blabel, capacity_mw=settings.power.building_capacity_mw
        )
        for sx, (slabel, n_cool, n_near, n_struct, n_empty) in enumerate(suites, start=1):
            sid = f"{bid}-s{sx}"
            suite = Suite(
                suite_id=sid, building_id=bid, ordinal=sx, label=slabel,
                capacity_mw=settings.power.suite_capacity_mw,
            )
            row_ord = 0

            def add_row(rack_types: list[str]) -> None:
                nonlocal row_ord
                row_ord += 1
                rid = f"{sid}-r{row_ord:02d}"
                row = Row(
                    row_id=rid, suite_id=sid, building_id=bid, ordinal=row_ord,
                    label=f"Row {row_ord:02d}", capacity_kw=settings.power.row_capacity_kw,
                )
                for px in range(positions_per_row):
                    pid = f"{rid}-p{px:02d}"
                    rtype = rack_types[px] if px < len(rack_types) else None
                    rack_id = None
                    if rtype is not None:
                        spec = catalog[rtype]
                        rack_id = f"rk-{rid}-p{px:02d}"
                        racks.append(
                            Rack(
                                rack_id=rack_id, position_id=pid, row_id=rid,
                                suite_id=sid, building_id=bid, rack_type=rtype,
                                family=spec.family, generation=spec.generation,
                                power_draw_kw=spec.power_draw_kw, state=RackState.ACTIVE,
                                installed_at=now, updated_at=now,
                            )
                        )
                    row.positions.append(
                        RackPosition(
                            position_id=pid, row_id=rid, suite_id=sid, building_id=bid,
                            ordinal=px, occupied=rtype is not None, rack_id=rack_id,
                        )
                    )
                suite.rows.append(row)

            for _ in range(n_cool):
                add_row(list(_DEMO_COOL_VARIANTS[cool_i % len(_DEMO_COOL_VARIANTS)]))
                cool_i += 1
            for _ in range(n_near):
                add_row(_DEMO_NEAR_FILLER + [_DEMO_NEAR_CANDIDATE])
            for _ in range(n_struct):
                add_row(list(_DEMO_STRUCT_FILLER))
            for _ in range(n_empty):
                add_row([])

            building.suites.append(suite)
        buildings.append(building)

    _ = seed  # deterministic; accepted only for CLI symmetry
    return GeneratedFloor(buildings=buildings, racks=racks)
