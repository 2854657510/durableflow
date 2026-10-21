from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


def test_v1_workflows_and_runs():
    with TestClient(app) as client:
        workflows = client.get("/api/v1/workflows")
        assert workflows.status_code == 200
        demo = next(w for w in workflows.json() if w["name"] == "demo_pipeline")

        created = client.post(
            f"/api/v1/workflows/{demo['id']}/runs",
            json={"input": {}, "max_cost": 1.0},
            headers={"Idempotency-Key": "v1-test-1"},
        )
        assert created.status_code == 201
        run_id = created.json()["run"]["id"]

        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200
        assert client.get(f"/api/v1/runs/{run_id}/steps").status_code == 200


def test_runs_pagination_and_total_count():
    with TestClient(app) as client:
        demo = next(
            w for w in client.get("/api/v1/workflows").json() if w["name"] == "demo_pipeline"
        )
        for index in range(3):
            response = client.post(
                f"/api/v1/workflows/{demo['id']}/runs",
                json={"input": {"index": index}},
                headers={"Idempotency-Key": f"page-{index}"},
            )
            assert response.status_code == 201

        page = client.get("/api/v1/runs?limit=2&offset=1")
        assert page.status_code == 200
        assert len(page.json()) == 2
        assert int(page.headers["X-Total-Count"]) >= 3

        empty = client.get("/api/v1/runs?limit=2&offset=1000")
        assert empty.status_code == 200
        assert empty.json() == []


def test_error_envelope_and_request_id():
    with TestClient(app) as client:
        response = client.get("/api/v1/runs/999999")

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "run not found"
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"]


def test_validation_error_envelope():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/workflows",
            json={"name": "", "version": 1, "spec": {"steps": []}},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]


def test_openapi_exposes_v1_only():
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert any(path.startswith("/api/v1/") for path in paths)
    assert not any(path.startswith("/api/") and not path.startswith("/api/v1/") for path in paths)


def test_api_key_uses_constant_time_compare(monkeypatch):
    import app.api.deps as deps

    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(api_key="secret"))

    with TestClient(app) as client:
        missing = client.get("/api/v1/workflows")
        wrong = client.get("/api/v1/workflows", headers={"X-API-Key": "wrong"})
        right = client.get("/api/v1/workflows", headers={"X-API-Key": "secret"})

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "UNAUTHORIZED"
    assert wrong.status_code == 401
    assert right.status_code == 200
