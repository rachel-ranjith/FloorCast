"""Topology reassembly: shape, occupant_rack_id -> rack_id mapping, empty cells.

Exercises the pure `assemble_topology` over synthetic DynamoDB items (the same
item shapes scripts/seed_data.py writes), so no DynamoDB is needed.
"""

from decimal import Decimal

from floorcast.services.topology_service import assemble_topology


def _items() -> list[dict]:
    """One building / one suite / one row with two positions: one occupied by a
    rack, one empty (no occupant attribute, exactly as seed_data writes it)."""
    return [
        {"entity": "building", "building_id": "b1", "label": "Building 1",
         "capacity_mw": Decimal("6.0")},
        {"entity": "suite", "suite_id": "b1-s1", "building_id": "b1",
         "label": "Suite 1", "ordinal": 1, "capacity_mw": Decimal("1.5")},
        {"entity": "row", "row_id": "b1-s1-r01", "suite_id": "b1-s1",
         "building_id": "b1", "label": "Row 01", "ordinal": 1,
         "capacity_kw": Decimal("90.0")},
        # occupied position — occupant pointer lives under occupant_rack_id
        {"entity": "position", "position_id": "b1-s1-r01-p00", "row_id": "b1-s1-r01",
         "suite_id": "b1-s1", "building_id": "b1", "ordinal": 0, "occupied": True,
         "occupant_rack_id": "rk-1"},
        # empty position — no occupant_rack_id attribute at all
        {"entity": "position", "position_id": "b1-s1-r01-p01", "row_id": "b1-s1-r01",
         "suite_id": "b1-s1", "building_id": "b1", "ordinal": 1, "occupied": False},
        # the rack occupying p00; generation stored as the gsi-generation PK
        {"entity": "rack", "rack_id": "rk-1", "rack_type": "compute-2023",
         "family": "compute", "generation": "GEN#2023",
         "power_draw_kw": Decimal("8.5")},
    ]


def test_topology_shape(settings):
    topo = assemble_topology(_items(), settings)

    assert len(topo.buildings) == 1
    b = topo.buildings[0]
    assert b.building_id == "b1"
    assert b.capacity_kw == 6000.0
    assert len(b.suites) == 1

    s = b.suites[0]
    assert s.suite_id == "b1-s1"
    assert s.capacity_kw == 1500.0
    assert len(s.rows) == 1

    row = s.rows[0]
    assert row.row_id == "b1-s1-r01"
    assert row.capacity_kw == 90.0
    assert len(row.positions) == 2
    # echoed for empty-row utilization math on the frontend
    assert topo.row_capacity_kw == settings.power.row_capacity_kw


def test_occupant_rack_id_maps_onto_rack_id_with_power(settings):
    topo = assemble_topology(_items(), settings)
    row = topo.buildings[0].suites[0].rows[0]
    occupied = next(p for p in row.positions if p.position_id == "b1-s1-r01-p00")

    assert occupied.occupied is True
    # the stored occupant_rack_id is surfaced as rack_id
    assert occupied.rack_id == "rk-1"
    # and the rack's power data is embedded for load colouring
    assert occupied.rack is not None
    assert occupied.rack.rack_type == "compute-2023"
    assert occupied.rack.generation == 2023
    assert occupied.rack.power_draw_kw == 8.5


def test_empty_position_returns_cleanly(settings):
    topo = assemble_topology(_items(), settings)
    row = topo.buildings[0].suites[0].rows[0]
    empty = next(p for p in row.positions if p.position_id == "b1-s1-r01-p01")

    assert empty.occupied is False
    assert empty.rack_id is None
    assert empty.rack is None


def test_power_rolls_up_for_load_colouring(settings):
    topo = assemble_topology(_items(), settings)
    b = topo.buildings[0]
    # one 8.5 kW rack -> row, suite, and building loads all equal 8.5
    assert b.suites[0].rows[0].load_kw == 8.5
    assert b.suites[0].load_kw == 8.5
    assert b.load_kw == 8.5


def test_empty_floor_returns_no_buildings(settings):
    assert assemble_topology([], settings).buildings == []
