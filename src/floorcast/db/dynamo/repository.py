"""Read/write helpers over the single Floorcast DynamoDB table.

Encapsulates the access patterns described in tables.py so services never build
keys by hand.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from config.settings import Settings, get_settings
from floorcast.db.dynamo import tables as T
from floorcast.db.dynamo.client import get_table
from floorcast.models.rack import Rack, RackState
from floorcast.optimizer.engine import FleetInput, TierCapacities


class FloorRepository:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.table = get_table(self.settings)

    # ----- racks ----------------------------------------------------------- #
    def list_racks_by_generation(self, generation: int) -> list[Rack]:
        resp = self.table.query(
            IndexName=self.settings.dynamo.gsi_generation,
            KeyConditionExpression=Key("generation").eq(T.generation_gsi_pk(generation)),
        )
        return [self._item_to_rack(i) for i in resp.get("Items", [])]

    def scan_racks(self) -> list[Rack]:
        """Full fleet scan (fine for seed-scale; paginate in prod-scale paths)."""
        racks: list[Rack] = []
        kwargs: dict = {"FilterExpression": Key("SK").eq("META")}
        # SK is the range key; use a filter on entity instead for clarity.
        kwargs = {}
        last = None
        while True:
            if last:
                kwargs["ExclusiveStartKey"] = last
            resp = self.table.scan(**kwargs)
            for item in resp.get("Items", []):
                if item.get("entity") == "rack":
                    racks.append(self._item_to_rack(item))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
        return racks

    def heatmap_buckets(self, building_id: str, band_kw: float = 10.0) -> dict[str, int]:
        """Count racks per power band in a building (drives the floor heatmap)."""
        counts: dict[str, int] = {}
        for rack in self.scan_racks():
            if rack.building_id != building_id:
                continue
            bucket = T.power_bucket(building_id, rack.power_draw_kw, band_kw)
            counts[bucket] = counts.get(bucket, 0) + 1
        return counts

    # ----- capacities ------------------------------------------------------ #
    def tier_capacities(self) -> TierCapacities:
        """Read per-tier capacities from the stored topology."""
        caps = TierCapacities()
        last = None
        kwargs: dict = {}
        while True:
            if last:
                kwargs["ExclusiveStartKey"] = last
            resp = self.table.scan(**kwargs)
            for item in resp.get("Items", []):
                entity = item.get("entity")
                if entity == "building":
                    caps.building_kw[item["building_id"]] = float(item["capacity_mw"]) * 1000.0
                elif entity == "suite":
                    caps.suite_kw[item["suite_id"]] = float(item["capacity_mw"]) * 1000.0
                elif entity == "row":
                    caps.row_kw[item["row_id"]] = float(item["capacity_kw"])
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
        return caps

    def load_fleet_input(self) -> FleetInput:
        return FleetInput(racks=self.scan_racks(), capacities=self.tier_capacities())

    # ----- mapping --------------------------------------------------------- #
    @staticmethod
    def _item_to_rack(item: dict) -> Rack:
        gen = item["generation"]
        gen_int = int(gen.split("#")[-1]) if isinstance(gen, str) and "#" in gen else int(gen)
        return Rack(
            rack_id=item["rack_id"],
            position_id=item["position_id"],
            row_id=item["row_id"],
            suite_id=item["suite_id"],
            building_id=item["building_id"],
            rack_type=item["rack_type"],
            family=item["family"],
            generation=gen_int,
            power_draw_kw=float(item["power_draw_kw"]),
            state=RackState(item.get("state", "active")),
            installed_at=_parse_dt(item.get("installed_at")),
            updated_at=_parse_dt(item.get("updated_at")),
        )


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (str,)):
        return datetime.fromisoformat(value)
    return datetime.now()
