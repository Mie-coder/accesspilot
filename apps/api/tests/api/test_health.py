from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_checks_the_database(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = TestClient(create_app(session_factory=database_session_factory))

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
