"""Unforgeable PostgreSQL session advisory-lock ownership for T33."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session


class AdvisoryLockOwnershipError(RuntimeError):
    """An operation required the active thread advisory-lock owner."""


@dataclass
class _LockState:
    active: bool = True


class AdvisoryLockHandle:
    """Opaque ownership of one session-level PostgreSQL advisory lock.

    Instances are created only by the lock service while the lock is actually
    held on a dedicated connection.  The handle becomes permanently invalid
    when the ``advisory_lock`` context exits, and it always knows the exact
    ``agent_thread_id`` it was acquired for.  There is no public ``_create`` or
    ``_token`` bypass: the constructor requires a private ``_LockState`` that
    only the lock service can obtain.
    """

    __slots__ = ("_state", "_session", "_agent_thread_id")

    def __init__(
        self,
        state: _LockState,
        session: Session,
        agent_thread_id: UUID,
    ) -> None:
        self._state = state
        self._session = session
        self._agent_thread_id = agent_thread_id

    @property
    def session(self) -> Session:
        self.require_active()
        return self._session

    @property
    def agent_thread_id(self) -> UUID:
        return self._agent_thread_id

    def require_active(self) -> None:
        if not self._state.active:
            raise AdvisoryLockOwnershipError(
                "advisory lock ownership is no longer active"
            )

    def require_thread(self, agent_thread_id: UUID) -> None:
        self.require_active()
        if self._agent_thread_id != agent_thread_id:
            raise AdvisoryLockOwnershipError(
                "advisory lock does not own the requested workspace thread"
            )

    def require_session(self, session: Session) -> None:
        self.require_active()
        if self._session is not session:
            raise AdvisoryLockOwnershipError(
                "operation must run on the advisory-lock owning session"
            )

    def invalidate(self) -> None:
        self._state.active = False


def _create_lock_handle(
    state: _LockState,
    session: Session,
    agent_thread_id: UUID,
) -> AdvisoryLockHandle:
    return AdvisoryLockHandle(state, session, agent_thread_id)
