from fastapi.testclient import TestClient

from app.main import app


def test_health_and_workflows():
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["database"] == "ok"

        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        names = [w["name"] for w in resp.json()]
        assert "demo_pipeline" in names
        assert "agent_review" in names


def test_create_and_query_run():
    with TestClient(app) as client:
        workflows = client.get("/api/workflows").json()
        demo = next(w for w in workflows if w["name"] == "demo_pipeline")

        resp = client.post(
            f"/api/workflows/{demo['id']}/runs",
            json={"input": {}, "max_cost": 1.0},
            headers={"Idempotency-Key": "api-test-1"},
        )
        assert resp.status_code == 201
        run = resp.json()["run"]
        assert run["status"] == "PENDING"

        again = client.post(
            f"/api/workflows/{demo['id']}/runs",
            json={"input": {}, "max_cost": 1.0},
            headers={"Idempotency-Key": "api-test-1"},
        )
        assert again.status_code == 200
        assert again.json()["created"] is False

        run_id = run["id"]
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        steps = client.get(f"/api/runs/{run_id}/steps").json()
        assert len(steps) == 3
        assert client.get(f"/api/runs/{run_id}/events").status_code == 200
        assert client.get(f"/api/runs/{run_id}/cost").status_code == 200

        canceled = client.post(f"/api/runs/{run_id}/cancel")
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "CANCELED"


def test_ui_renders():
    with TestClient(app) as client:
        resp = client.get("/ui")
        assert resp.status_code == 200
        assert "DurableFlow" in resp.text
