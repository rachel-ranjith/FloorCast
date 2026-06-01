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
