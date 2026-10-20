from fastapi.testclient import TestClient

from app.main import app


def test_healthz() -> None:
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_reports_checks() -> None:
    with TestClient(app) as client:
        r = client.get("/readyz")
    assert r.status_code in (200, 503)
    assert set(r.json()["checks"]) == {"postgres", "redis"}
