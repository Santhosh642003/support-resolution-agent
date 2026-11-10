from fastapi.testclient import TestClient

from app.main import app


def test_health_ok_and_labeled_simulated() -> None:
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_is_simulated"] is True
    assert body["llm_provider"] == "fake"  # default must be offline
