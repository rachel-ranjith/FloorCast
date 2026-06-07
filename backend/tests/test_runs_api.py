"""Runs read endpoints: list ordering/shape, 404 on unknown id, 422 on bad id.

The RunService is swapped for an in-memory fake via dependency override, so these
tests exercise the routing/serialization contract without Postgres.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from floorcast.api.main import app
from floorcast.api.routers import runs as runs_router
from floorcast.api.schemas import RunDetail, RunSummary, ScheduleItemOut, ScheduleOut
from floorcast.services.run_service import RunNotFoundError


class _FakeRunService:
    """Returns canned runs, newest first, and 404s for anything unknown."""

    def __init__(self):
        self._runs = [
            RunSummary(
                run_id="11111111-1111-1111-1111-111111111111",
                name="newest",
                status="optimal",
                total_swaps=12,
                total_row_overage_kw=0.0,
                created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            ),
            RunSummary(
                run_id="22222222-2222-2222-2222-222222222222",
                name="oldest",
                status="feasible",
                total_swaps=5,
                total_row_overage_kw=3.5,
                created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            ),
        ]

    def list_runs(self, limit=50, offset=0):
        return self._runs[offset : offset + limit]

    def get_run(self, run_id):
        for r in self._runs:
            if str(run_id) == r.run_id:
                return RunDetail(
                    run_id=r.run_id, name=r.name, status=r.status,
                    horizon_months=12, total_swaps=r.total_swaps,
                    total_row_overage_kw=r.total_row_overage_kw,
                    created_at=r.created_at,
                    schedule=ScheduleOut(
                        schedule_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        horizon_months=12, total_swaps=r.total_swaps,
                        items_by_month={
                            "1": [
                                ScheduleItemOut(
                                    item_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                                    month=1, position_id="b1-s1-r01-p00",
                                    row_id="b1-s1-r01", suite_id="b1-s1",
                                    building_id="b1", from_rack_type="compute-2023",
                                    to_rack_type="compute-2025", from_power_kw=8.5,
                                    to_power_kw=12.0,
                                )
                            ]
                        },
                    ),
                )
        raise RunNotFoundError(run_id)


@pytest.fixture
def client():
    app.dependency_overrides[runs_router.get_run_service] = _FakeRunService
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_runs_most_recent_first(client):
    resp = client.get("/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50 and body["offset"] == 0
    names = [r["name"] for r in body["runs"]]
    assert names == ["newest", "oldest"]
    # shape the frontend list relies on
    first = body["runs"][0]
    assert set(first) >= {
        "run_id", "name", "status", "total_swaps", "total_row_overage_kw", "created_at"
    }


def test_get_run_returns_schedule_grouped_by_month(client):
    resp = client.get("/runs/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "newest"
    assert "1" in body["schedule"]["items_by_month"]
    assert body["schedule"]["items_by_month"]["1"][0]["to_rack_type"] == "compute-2025"


def test_get_run_unknown_id_returns_404(client):
    resp = client.get("/runs/99999999-9999-9999-9999-999999999999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_get_run_malformed_id_returns_422(client):
    # A non-UUID id is a clean validation error, never a 500.
    resp = client.get("/runs/not-a-uuid")
    assert resp.status_code == 422
