"""Assembles the full floor topology for the frontend.

Reads every item from the single DynamoDB table (paginated) and stitches the
Building > Suite > Row > Position hierarchy back together, embedding each
occupied position's current rack (type / generation / power) so the frontend can
render the floor *and* colour it by load.

The reassembly itself (`assemble_topology`) is a pure function over raw DynamoDB
items so it can be unit-tested without DynamoDB.
"""

from __future__ import annotations

from config.settings import Settings, get_settings
from floorcast.api.schemas import (
    BuildingNode,
    PositionNode,
    RackSummary,
    RowNode,
    SuiteNode,
    TopologyResponse,
)
from floorcast.db.dynamo.repository import FloorRepository


def _gen_int(value) -> int:
    """Racks store generation as the gsi-generation PK (e.g. 'GEN#2024')."""
    if isinstance(value, str) and "#" in value:
        return int(value.split("#")[-1])
    return int(value)


def _rack_summary(item: dict) -> RackSummary:
    return RackSummary(
        rack_id=item["rack_id"],
        rack_type=item["rack_type"],
        family=item["family"],
        generation=_gen_int(item["generation"]),
        power_draw_kw=float(item["power_draw_kw"]),
    )


def assemble_topology(items: list[dict], settings: Settings) -> TopologyResponse:
    """Stitch raw DynamoDB items into the nested topology response.

    Pure: given the same items + settings it always returns the same shape.
    """
    buildings: dict[str, BuildingNode] = {}
    suites: dict[str, SuiteNode] = {}
    rows: dict[str, RowNode] = {}
    # suite_id -> building_id, row_id -> suite_id, so we can nest in a second pass.
    suite_parent: dict[str, str] = {}
    row_parent: dict[str, str] = {}
    racks_by_id: dict[str, dict] = {}
    position_items: list[dict] = []

    for item in items:
        entity = item.get("entity")
        if entity == "building":
            bid = item["building_id"]
            cap_mw = float(item["capacity_mw"])
            buildings[bid] = BuildingNode(
                building_id=bid,
                label=item.get("label", bid),
                capacity_mw=cap_mw,
                capacity_kw=cap_mw * 1000.0,
                load_kw=0.0,
            )
        elif entity == "suite":
            sid = item["suite_id"]
            cap_mw = float(item["capacity_mw"])
            suites[sid] = SuiteNode(
                suite_id=sid,
                label=item.get("label", sid),
                ordinal=int(item.get("ordinal", 0)),
                capacity_mw=cap_mw,
                capacity_kw=cap_mw * 1000.0,
                load_kw=0.0,
            )
            suite_parent[sid] = item["building_id"]
        elif entity == "row":
            rid = item["row_id"]
            rows[rid] = RowNode(
                row_id=rid,
                label=item.get("label", rid),
                ordinal=int(item.get("ordinal", 0)),
                capacity_kw=float(item["capacity_kw"]),
                load_kw=0.0,
            )
            row_parent[rid] = item["suite_id"]
        elif entity == "position":
            position_items.append(item)
        elif entity == "rack":
            racks_by_id[item["rack_id"]] = item

    # Attach positions to their rows, mapping occupant_rack_id -> rack_id and
    # embedding the rack summary. Empty positions (no occupant attribute) come
    # back occupied=false / rack=null.
    for item in position_items:
        rid = item["row_id"]
        row = rows.get(rid)
        if row is None:
            continue  # orphan position with no parent row — skip defensively
        occupant_id = item.get("occupant_rack_id")
        rack_item = racks_by_id.get(occupant_id) if occupant_id else None
        rack = _rack_summary(rack_item) if rack_item is not None else None
        if rack is not None:
            row.load_kw += rack.power_draw_kw
        row.positions.append(
            PositionNode(
                position_id=item["position_id"],
                ordinal=int(item.get("ordinal", 0)),
                # Occupancy is derived from the occupant pointer, not the stored
                # `occupied` flag, so a position is "empty" iff it has no rack.
                occupied=occupant_id is not None,
                rack_id=occupant_id,
                rack=rack,
            )
        )

    # Nest rows under suites and suites under buildings, rolling load upward.
    for rid, row in rows.items():
        row.positions.sort(key=lambda p: p.ordinal)
        sid = row_parent.get(rid)
        suite = suites.get(sid) if sid else None
        if suite is not None:
            suite.rows.append(row)
            suite.load_kw += row.load_kw

    for sid, suite in suites.items():
        suite.rows.sort(key=lambda r: r.ordinal)
        bid = suite_parent.get(sid)
        building = buildings.get(bid) if bid else None
        if building is not None:
            building.suites.append(suite)
            building.load_kw += suite.load_kw

    for building in buildings.values():
        building.suites.sort(key=lambda s: s.ordinal)

    ordered = [buildings[k] for k in sorted(buildings)]
    return TopologyResponse(
        buildings=ordered,
        row_capacity_kw=settings.power.row_capacity_kw,
    )


class TopologyService:
    def __init__(self, settings: Settings | None = None, repo: FloorRepository | None = None):
        self.settings = settings or get_settings()
        self.repo = repo or FloorRepository(self.settings)

    def get_topology(self) -> TopologyResponse:
        return assemble_topology(self.repo.scan_all_items(), self.settings)
