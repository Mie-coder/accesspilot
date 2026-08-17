"""Unforgeable PostgreSQL session advisory-lock ownership for T33."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session


class AdvisoryLockOwnershipError(RuntimeError):
    """An operation required the active thread advisory-lock owner."""


_SERVER_LOCK_TOKEN = object()


@dataclass(frozen=True, init=False)
class AdvisoryLockHandle:
    """Opaque ownership of one session-level PostgreSQL advisory lock.

    Instances can only be created by the service that acquired the lock, so a
    client cannot forge ownership.  The handle carries the exact Session that
    holds the lock; takeover, graph run, head promotion and finalize must all
    occur inside the same ``with service.advisory_lock(...)`` block.
    """

    session: Session
    agent_thread_id: UUID

    def __init__(
        self,
        *,
        session: Session,
        agent_thread_id: UUID,
        _token: object,
    ) -> None:
        if _token is not _SERVER_LOCK_TOKEN:
            raise TypeError("advisory lock ownership must come from the server")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "agent_thread_id", agent_thread_id)

    @classmethod
    def _create(cls, session: Session, agent_thread_id: UUID) -> AdvisoryLockHandle:
        return cls(
            session=session,
            agent_thread_id=agent_thread_id,
            _token=_SERVER_LOCK_TOKEN,
        )

    def require_session(self, session: Session) -> None:
        """Fail unless the caller is using the exact lock-holding session."""
        if self.session is not session:
            raise AdvisoryLockOwnershipError(
                "operation must run on the advisory-lock owning session"
            )

    @property
    def _token(self) -> Any:  # pragma: no cover - compatibility guard
        return _SERVER_LOCK_TOKEN
