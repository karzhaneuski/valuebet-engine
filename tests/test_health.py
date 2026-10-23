"""Health-check endpoints of the phase-1 service stubs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app as api_app
from core.scheduler.main import app as scheduler_app
from mcp_server.main import app as mcp_app


def test_api_health() -> None:
    client = TestClient(api_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_scheduler_health() -> None:
    client = TestClient(scheduler_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "scheduler"


def test_mcp_health() -> None:
    client = TestClient(mcp_app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "mcp"
