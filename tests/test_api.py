"""API tests exercising the FastAPI app against the live test database.

Uses httpx with a real database session — validation, idempotency, pagination,
filters, cancellation/retry flows and stats are all verified end-to-end.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient

from taskmesh.api import app, get_db
from taskmesh.models import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture()
def client(session_factory):
    def override_get_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


class TestSubmission:
    def test_submit_returns_201(self, client):
        r = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 5}})
        assert r.status_code == 201
        body = r.json()
        assert body["created"] is True
        assert body["task"]["status"] == "PENDING"
        assert body["task"]["priority"] == "NORMAL"

    def test_submit_scheduled(self, client):
        run_at = (utcnow() + dt.timedelta(hours=1)).isoformat()
        r = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}, "run_at": run_at})
        assert r.status_code == 201
        assert r.json()["task"]["status"] == "SCHEDULED"

    def test_unknown_task_type_422(self, client):
        r = client.post("/tasks", json={"task_type": "nope", "payload": {"operation": "sum", "n": 3}})
        assert r.status_code == 422

    def test_invalid_payload_422(self, client):
        r = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": "abc"}})
        assert r.status_code == 422

    def test_invalid_priority_422(self, client):
        r = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}, "priority": "URGENT"})
        assert r.status_code == 422

    def test_oversized_payload_413(self, client):
        r = client.post("/tasks", json={"task_type": "compute", "payload": {"blob": "x" * 20000}})
        assert r.status_code == 413

    def test_idempotent_resubmission(self, client):
        key = f"idem-{uuid.uuid4()}"
        r1 = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}, "idempotency_key": key})
        r2 = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}, "idempotency_key": key})
        assert r1.status_code == 201 and r2.status_code == 201
        assert r1.json()["created"] is True and r2.json()["created"] is False
        assert r1.json()["task"]["id"] == r2.json()["task"]["id"]


class TestRetrieval:
    def test_get_task_404(self, client):
        r = client.get(f"/tasks/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_list_pagination_and_total(self, client):
        for i in range(5):
            client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": i}})
        r = client.get("/tasks", params={"limit": 2, "offset": 0})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["total"] >= 5
        page2 = client.get("/tasks", params={"limit": 2, "offset": 2}).json()
        assert page2["items"][0]["id"] != body["items"][0]["id"]

    def test_filters(self, client):
        client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}, "priority": "HIGH"})
        r = client.get("/tasks", params={"priority": "HIGH"})
        assert all(t["priority"] == "HIGH" for t in r.json()["items"])
        r = client.get("/tasks", params={"status": "PENDING"})
        assert all(t["status"] == "PENDING" for t in r.json()["items"])
        r = client.get("/tasks", params={"task_type": "compute"})
        assert all(t["task_type"] == "compute" for t in r.json()["items"])
        r = client.get("/tasks", params={"status": "NOT_A_STATE"})
        assert r.status_code == 422

    def test_attempts_endpoint_404_and_history(self, client):
        assert client.get(f"/tasks/{uuid.uuid4()}/attempts").status_code == 404
        r = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}})
        tid = r.json()["task"]["id"]
        hist = client.get(f"/tasks/{tid}/attempts")
        assert hist.status_code == 200 and hist.json() == []


class TestCancelRetry:
    def test_cancel_pending(self, client):
        tid = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}}).json()["task"]["id"]
        r = client.post(f"/tasks/{tid}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
        # Idempotent: cancelling again returns the terminal state.
        r2 = client.post(f"/tasks/{tid}/cancel")
        assert r2.status_code == 200 and r2.json()["status"] == "CANCELLED"

    def test_retry_wrong_state_409(self, client):
        tid = client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}}).json()["task"]["id"]
        r = client.post(f"/tasks/{tid}/retry")
        assert r.status_code == 409


class TestOps:
    def test_health(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_ready(self, client):
        r = client.get("/ready")
        assert r.status_code == 200 and r.json()["status"] == "ready"

    def test_stats(self, client):
        client.post("/tasks", json={"task_type": "compute", "payload": {"operation": "sum", "n": 3}, "priority": "HIGH"})
        r = client.get("/stats")
        body = r.json()
        assert "PENDING" in body["task_counts_by_status"]
        assert set(body["queue_depth_by_priority"]) == {"HIGH", "NORMAL", "LOW"}
        assert "compute" in body["registered_task_types"]

    def test_openapi_available(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/tasks" in paths and "/tasks/{task_id}/cancel" in paths


class TestConcurrentDuplicateSubmission:
    def test_concurrent_same_idempotency_key_creates_one_task(self, client):
        """Racing duplicate submissions must yield exactly one logical task."""
        import concurrent.futures as cf

        key = f"idem-conc-{uuid.uuid4()}"
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futures = [
                ex.submit(
                    client.post,
                    "/tasks",
                    json={"task_type": "compute", "payload": {"operation": "sum", "n": 1}, "idempotency_key": key},
                )
                for _ in range(8)
            ]
            responses = [f.result() for f in futures]
        ids = {r.json()["task"]["id"] for r in responses if r.status_code == 201}
        created_flags = [r.json()["created"] for r in responses if r.status_code == 201]
        assert len(ids) == 1, f"expected one task id, got {ids}"
        assert created_flags.count(True) == 1, "exactly one submission may report created=True"
