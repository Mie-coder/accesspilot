"""T19 Mock Login, AuthSession and request Principal boundaries."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import AuthSessionRecord, EmployeeRecord, WorkspaceRecord
from accesspilot.db.workspace_store import hash_workspace_token

# This allowlist is deliberately independent from the employee table.  The
# seeded catalog may contain additional fictional employees, but they cannot
# enter the product through Mock Login.
MOCK_LOGIN_ACCOUNTS: dict[str, str] = {
    "EMP-001": "requester",
    "EMP-002": "manager",
    "EMP-003": "data_owner",
    "EMP-004": "permissions_admin",
}


def hash_secret(value: str) -> str:
    """Hash a bearer or CSRF secret before persistence/comparison."""

    return sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Trusted identity derived from AuthSession -> EmployeeRecord."""

    employee_id: str
    name: str
    department: str
    roles: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "employee_id": self.employee_id,
            "name": self.name,
            "department": self.department,
            "roles": list(self.roles),
        }


@dataclass(frozen=True)
class AuthContext:
    """Valid request session plus the raw token used only for workspace lookup."""

    session_id: UUID
    token: str
    csrf_hash: str
    workspace_id: UUID
    principal: Principal
    expires_at: datetime


class LoginAccountError(ValueError):
    """The requested account is outside the fixed Mock Login allowlist."""


class InvalidAuthSessionError(ValueError):
    """Cookie is missing, malformed, revoked or expired."""


class CsrfMismatchError(ValueError):
    """The supplied CSRF token does not match the current session hash."""


def _principal_for_employee(employee: EmployeeRecord) -> Principal:
    # Roles are always read from EmployeeRecord.  The allowlist only chooses
    # an employee; it never accepts a role supplied by the request.
    return Principal(
        employee_id=employee.employee_id,
        name=employee.name,
        department=employee.department,
        roles=tuple(sorted(employee.roles)),
    )


def create_login_session(
    session_factory: sessionmaker[Session],
    *,
    account_id: str,
    ttl_seconds: int,
    flow_allocator: Callable[[UUID], int] | None = None,
) -> tuple[AuthContext, str, str]:
    """Atomically create a private Workspace and AuthSession.

    Returns the context and the two one-time plaintext values to put in
    response cookies/JSON.  The transaction commits both records together.
    """

    if account_id not in MOCK_LOGIN_ACCOUNTS:
        raise LoginAccountError(account_id)
    token = token_urlsafe(48)
    csrf_token = token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    with session_factory() as session:
        employee = session.get(EmployeeRecord, account_id)
        if employee is None:
            # A configured allowlist entry must still be seeded in the
            # employee directory; this is a deployment/data error, not a
            # reason to fall back to request-provided identity.
            raise LoginAccountError(account_id)
        agent_thread_id = uuid4()
        workspace = WorkspaceRecord(
            token_hash=hash_workspace_token(token),
            agent_thread_id=agent_thread_id,
            flow_version=(flow_allocator or (lambda thread_id: 1))(
                agent_thread_id
            ),
            actor_id=employee.employee_id,
            demo_actor_id=None,
            demo_session_active=False,
            draft=None,
            fault_mode=None,
        )
        session.add(workspace)
        session.flush()
        auth_session = AuthSessionRecord(
            token_hash=hash_secret(token),
            employee_id=employee.employee_id,
            workspace_id=workspace.id,
            csrf_hash=hash_secret(csrf_token),
            expires_at=expires_at,
        )
        session.add(auth_session)
        session.commit()
        context = AuthContext(
            session_id=auth_session.id,
            token=token,
            csrf_hash=auth_session.csrf_hash,
            workspace_id=workspace.id,
            principal=_principal_for_employee(employee),
            expires_at=expires_at,
        )
    return context, token, csrf_token


def load_auth_context(
    session_factory: sessionmaker[Session],
    *,
    token: str | None,
) -> AuthContext:
    """Resolve a bearer cookie to AuthSession then EmployeeRecord."""

    if not token:
        raise InvalidAuthSessionError("missing")
    with session_factory() as session:
        row = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == hash_secret(token)
            )
        )
        if row is None or row.revoked_at is not None:
            raise InvalidAuthSessionError("invalid")
        now = datetime.now(UTC)
        expires_at = row.expires_at
        if expires_at <= now:
            raise InvalidAuthSessionError("expired")
        employee = session.get(EmployeeRecord, row.employee_id)
        workspace = session.get(WorkspaceRecord, row.workspace_id)
        if employee is None or workspace is None or workspace.actor_id != employee.employee_id:
            raise InvalidAuthSessionError("binding")
        return AuthContext(
            session_id=row.id,
            token=token,
            csrf_hash=row.csrf_hash,
            workspace_id=row.workspace_id,
            principal=_principal_for_employee(employee),
            expires_at=expires_at,
        )


def rotate_csrf(
    session_factory: sessionmaker[Session],
    *,
    context: AuthContext,
) -> str:
    """Rotate the CSRF secret while preserving the AuthSession principal."""

    csrf_token = token_urlsafe(32)
    with session_factory() as session:
        row = session.get(AuthSessionRecord, context.session_id)
        if row is None or row.revoked_at is not None:
            raise InvalidAuthSessionError("invalid")
        if row.expires_at <= datetime.now(UTC):
            raise InvalidAuthSessionError("expired")
        row.csrf_hash = hash_secret(csrf_token)
        session.commit()
    return csrf_token


def verify_csrf(
    session_factory: sessionmaker[Session],
    *,
    context: AuthContext,
    token: str | None,
) -> None:
    """Constant-time-ish hash comparison against the server-stored value."""

    if not token or not compare_digest(hash_secret(token), context.csrf_hash):
        raise CsrfMismatchError("mismatch")


def revoke_session(
    session_factory: sessionmaker[Session],
    *,
    context: AuthContext,
) -> None:
    """Revoke only the AuthSession; the private Workspace remains intact."""

    with session_factory() as session:
        row = session.get(AuthSessionRecord, context.session_id)
        if row is None:
            raise InvalidAuthSessionError("invalid")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            session.commit()
