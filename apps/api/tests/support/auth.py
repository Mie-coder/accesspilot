"""Small AuthSession helper used by API/evaluation tests.

The production API deliberately has no test-only identity switch.  Tests that
need another role create another ``TestClient`` and log that client in through
the same public Mock Login route as a browser would.
"""

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.auth import hash_secret
from accesspilot.db.models import AuthSessionRecord

ORIGIN = "http://127.0.0.1:5173"


@dataclass(frozen=True)
class LoginResult:
    """The public login facts useful to a test."""

    account_id: str
    csrf_token: str
    session_token: str
    principal: dict[str, Any]
    session_id: str | None = None


def login_as(
    client: TestClient,
    account_id: str = "EMP-001",
    *,
    origin: str = ORIGIN,
    session_factory: sessionmaker[Session] | None = None,
) -> LoginResult:
    """Log a client in and install Origin/CSRF defaults for subsequent calls."""

    response = client.post(
        "/api/auth/login",
        json={"account_id": account_id},
        headers={"Origin": origin},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    csrf_token = payload["csrf_token"]
    session_token = client.cookies.get("accesspilot_session")
    assert isinstance(csrf_token, str) and csrf_token
    assert isinstance(session_token, str) and session_token
    principal = payload["principal"]
    assert isinstance(principal, dict)
    client.headers.update({"Origin": origin, "X-CSRF-Token": csrf_token})
    session_id: str | None = None
    if session_factory is not None:
        with session_factory() as session:
            row = session.scalar(
                select(AuthSessionRecord).where(
                    AuthSessionRecord.token_hash == hash_secret(session_token)
                )
            )
            assert row is not None
            session_id = str(row.id)
    return LoginResult(
        account_id=account_id,
        csrf_token=csrf_token,
        session_token=session_token,
        principal=principal,
        session_id=session_id,
    )
