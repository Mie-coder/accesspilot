"""T33 turn execution lease, fence, advisory lock, and takeover primitives.

This module owns the application-side lifecycle of one logical LangGraph
input.  It does not wire the production JSON/SSE entry points; T38/T40 own
that integration.  It deliberately follows the T32 canonical lock order
``execution -> workspace`` so takeover/complete never introduce a reverse
``workspace -> execution`` deadlock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.advisory_lock import (
    AdvisoryLockHandle,
    _create_lock_handle,
    _LockState,
)
from accesspilot.agent.checkpoint import CheckpointLocator, SaverLike
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
    utc_now,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.events import stage_workspace_event
from accesspilot.workspaces import UnknownWorkspaceError


class TurnExecutionError(RuntimeError):
    """Base class for stable turn execution failures."""

    code = "TURN_EXECUTION_ERROR"


class TurnInProgressError(TurnExecutionError):
    """Another running turn already owns this Workspace."""

    code = "TURN_IN_PROGRESS"


class TurnRecoveryInProgressError(TurnExecutionError):
    """An expired running turn is waiting for recovery takeover."""

    code = "TURN_RECOVERY_IN_PROGRESS"


class TurnLockUnavailableError(TurnExecutionError):
    """Another connection holds the Workspace thread advisory lock."""

    code = "TURN_LOCK_UNAVAILABLE"


class StaleTurnFenceError(TurnExecutionError):
    """The caller no longer owns the active execution/fence."""

    code = "STALE_TURN_FENCE"


class TurnLeaseActiveError(TurnExecutionError):
    """Takeover was attempted before the current lease expired."""

    code = "TURN_LEASE_ACTIVE"


class TurnNotFoundError(TurnExecutionError):
    """The requested logical execution does not exist."""

    code = "TURN_NOT_FOUND"


@dataclass(frozen=True)
class TurnExecutionHandle:
    """Immutable server-owned coordinates for one accepted logical input."""

    execution_id: UUID
    workspace_id: UUID
    agent_thread_id: UUID
    graph_run_id: UUID
    checkpoint_thread_id: str
    input_seq: int
    input_turn_id: str
    input_event_id: int
    attempt: int
    lease_fence: int
    lease_expires_at: datetime
    actor_id: str
    auth_session_ref: UUID
    accepted_checkpoint_id: str | None = None


@dataclass(frozen=True)
class RecoveryPlan:
    """What a takeover may safely resume from."""

    execution_id: UUID
    workspace_id: UUID
    agent_thread_id: UUID
    graph_run_id: UUID
    checkpoint_thread_id: str
    input_seq: int
    input_turn_id: str
    input_event_id: int
    attempt: int
    lease_fence: int
    lease_expires_at: datetime
    source: Literal["checkpoint", "input_event"]
    accepted_checkpoint_id: str | None = None


class TurnExecutionService:
    """Application turn lifecycle: begin, heartbeat, lock, takeover, complete."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds

    # ------------------------------------------------------------------
    # Begin input
    # ------------------------------------------------------------------
    def begin_input(
        self,
        *,
        workspace_token: str,
        auth_session_ref: UUID,
        actor_id: str,
        safe_user_text: str,
    ) -> TurnExecutionHandle:
        """Atomically allocate run/seq/turn/fence and persist input facts.

        The transaction holds the Workspace row lock, rejects a second running
        input before any user-visible fact is written, and only then stages
        ``turn.started`` + ``message.user`` and creates the execution row.
        """
        safe_text = redact_sensitive_content(safe_user_text.strip())
        if not safe_text:
            raise ValueError("safe_user_text must not be empty after cleaning")
        with self._session_factory() as session, session.begin():
            workspace = session.scalar(
                select(WorkspaceRecord)
                .where(WorkspaceRecord.token_hash == hash_workspace_token(workspace_token))
                .with_for_update()
            )
            if workspace is None:
                raise UnknownWorkspaceError(workspace_token)
            now = datetime.now(UTC)
            self._validate_auth_session(
                session,
                workspace_id=workspace.id,
                auth_session_ref=auth_session_ref,
                actor_id=actor_id,
                now=now,
            )
            running = session.scalar(
                select(AgentTurnExecutionRecord)
                .where(
                    AgentTurnExecutionRecord.workspace_id == workspace.id,
                    AgentTurnExecutionRecord.status == "running",
                )
                .limit(1)
            )
            if running is not None:
                if (
                    running.lease_expires_at is not None
                    and running.lease_expires_at <= now
                ):
                    raise TurnRecoveryInProgressError(
                        "recovery takeover is required before a new input"
                    )
                raise TurnInProgressError("another turn is already running")

            latest = session.scalar(
                select(AgentTurnExecutionRecord)
                .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
                .order_by(
                    AgentTurnExecutionRecord.input_seq.desc(),
                    AgentTurnExecutionRecord.created_at.desc(),
                )
                .limit(1)
            )
            if latest is not None:
                graph_run_id = latest.graph_run_id
                input_seq = latest.input_seq + 1
            else:
                graph_run_id = uuid4()
                input_seq = 0

            workspace.lease_fence += 1
            lease_fence = workspace.lease_fence
            checkpoint_thread_id = f"accesspilot:v1.3:{graph_run_id}"
            input_turn_id = f"turn-{uuid4()}"
            lease_expires_at = now + timedelta(seconds=self._lease_seconds)

            stage_workspace_event(
                session,
                workspace_token=workspace_token,
                event_type="turn.started",
                payload={
                    "turn_id": input_turn_id,
                    "lease_expires_at": lease_expires_at,
                },
            )
            user_message = stage_workspace_event(
                session,
                workspace_token=workspace_token,
                event_type="message.user",
                payload={
                    "content": safe_text,
                    "turn_id": input_turn_id,
                },
            )
            session.flush()

            execution = AgentTurnExecutionRecord(
                id=uuid4(),
                workspace_id=workspace.id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=checkpoint_thread_id,
                input_seq=input_seq,
                input_turn_id=input_turn_id,
                input_event_id=user_message.id,
                auth_session_ref=auth_session_ref,
                actor_id=actor_id,
                engine="langgraph",
                attempt=1,
                lease_fence=lease_fence,
                lease_expires_at=lease_expires_at,
                status="running",
                checkpoint_ns="",
                accepted_checkpoint_id=None,
                terminal_event_id=None,
            )
            session.add(execution)
            session.flush()
            execution_id = execution.id
            workspace_id = workspace.id
            agent_thread_id = workspace.agent_thread_id
            input_event_id = user_message.id

        return TurnExecutionHandle(
            execution_id=execution_id,
            workspace_id=workspace_id,
            agent_thread_id=agent_thread_id,
            graph_run_id=graph_run_id,
            checkpoint_thread_id=checkpoint_thread_id,
            input_seq=input_seq,
            input_turn_id=input_turn_id,
            input_event_id=input_event_id,
            attempt=1,
            lease_fence=lease_fence,
            lease_expires_at=lease_expires_at,
            actor_id=actor_id,
            auth_session_ref=auth_session_ref,
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------
    def heartbeat(self, handle: TurnExecutionHandle) -> datetime:
        """CAS-renew the lease only for the exact active owner."""
        now = datetime.now(UTC)
        new_expiry = now + timedelta(seconds=self._lease_seconds)
        with self._session_factory() as session, session.begin():
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(AgentTurnExecutionRecord)
                    .where(
                        AgentTurnExecutionRecord.id == handle.execution_id,
                        AgentTurnExecutionRecord.workspace_id == handle.workspace_id,
                        AgentTurnExecutionRecord.graph_run_id == handle.graph_run_id,
                        AgentTurnExecutionRecord.input_seq == handle.input_seq,
                        AgentTurnExecutionRecord.input_turn_id == handle.input_turn_id,
                        AgentTurnExecutionRecord.actor_id == handle.actor_id,
                        AgentTurnExecutionRecord.auth_session_ref
                        == handle.auth_session_ref,
                        AgentTurnExecutionRecord.lease_fence == handle.lease_fence,
                        AgentTurnExecutionRecord.status == "running",
                        AgentTurnExecutionRecord.lease_expires_at.is_not(None),
                        AgentTurnExecutionRecord.lease_expires_at > now,
                    )
                    .values(lease_expires_at=new_expiry, updated_at=utc_now())
                ),
            )
            if result.rowcount != 1:
                raise StaleTurnFenceError("heartbeat lease CAS failed")
        return new_expiry

    # ------------------------------------------------------------------
    # Advisory lock
    # ------------------------------------------------------------------
    @contextmanager
    def advisory_lock(
        self,
        agent_thread_id: UUID,
        *,
        timeout_seconds: float = 0.0,
    ) -> Iterator[AdvisoryLockHandle]:
        """Hold a PostgreSQL session advisory lock for one graph execution.

        The lock is acquired on a dedicated connection that is kept checked out
        across multiple application transactions.  This allows takeover to
        commit before graph execution while the session-level advisory lock is
        still held, and finalize to commit before the lock is released.
        """
        engine = self._session_factory.kw["bind"]
        connection = engine.connect()
        session = Session(bind=connection, expire_on_commit=False)
        key = f"accesspilot-turn:{agent_thread_id}"
        try:
            if timeout_seconds > 0:
                session.execute(
                    text("SET LOCAL lock_timeout = :milliseconds"),
                    {"milliseconds": int(timeout_seconds * 1000)},
                )
            acquired = session.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": key},
            ).scalar_one()
            if not acquired:
                raise TurnLockUnavailableError("thread advisory lock is held elsewhere")
            # Commit the lock-acquisition transaction while keeping the
            # dedicated connection checked out; the session-level advisory lock
            # remains held and later application transactions can begin cleanly.
            session.commit()
            state = _LockState()
            handle = _create_lock_handle(state, session, agent_thread_id)
            try:
                yield handle
            finally:
                handle.invalidate()
                session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
        finally:
            session.close()
            connection.close()

    # ------------------------------------------------------------------
    # Takeover
    # ------------------------------------------------------------------
    def takeover(
        self,
        *,
        workspace_token: str,
        graph_run_id: UUID,
        input_seq: int,
        auth_session_ref: UUID,
        actor_id: str,
        lock: AdvisoryLockHandle,
        saver: SaverLike | None = None,
    ) -> RecoveryPlan:
        """Take over an expired running execution and reuse its logical input.

        The caller must hold the advisory lock produced by ``advisory_lock``;
        the takeover transaction runs on that same lock-holding Session so the
        lock continuously covers takeover, the subsequent graph run, and the
        final head promotion/terminal write.

        Lock order is execution -> workspace, matching T32.  The returned plan
        chooses ``checkpoint`` when the current execution, any linked pending
        row, or any historical execution in the same ``graph_run_id`` already
        has an accepted head; otherwise it chooses the safe ``input_event``.
        When ``saver`` is supplied and a checkpoint source is selected, the
        exact head must exist or takeover fails closed.
        """
        session = lock.session
        now = datetime.now(UTC)
        with session.begin():
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
                )
            )
            if workspace is None:
                raise UnknownWorkspaceError(workspace_token)
            workspace_id = workspace.id

            execution = session.scalar(
                select(AgentTurnExecutionRecord)
                .where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id,
                    AgentTurnExecutionRecord.graph_run_id == graph_run_id,
                    AgentTurnExecutionRecord.input_seq == input_seq,
                )
                .with_for_update()
            )
            if execution is None:
                raise TurnNotFoundError("execution does not exist")
            if (
                execution.auth_session_ref != auth_session_ref
                or execution.actor_id != actor_id
            ):
                raise TurnExecutionError("takeover owner does not match execution")
            self._validate_auth_session(
                session,
                workspace_id=workspace_id,
                auth_session_ref=auth_session_ref,
                actor_id=actor_id,
                now=now,
            )
            if execution.status != "running" or execution.terminal_event_id is not None:
                raise TurnExecutionError("execution is not recoverable")
            if (
                execution.lease_expires_at is None
                or execution.lease_expires_at > now
            ):
                raise TurnLeaseActiveError("lease has not expired")

            accepted = execution.accepted_checkpoint_id
            if accepted is None:
                historical = session.scalar(
                    select(AgentTurnExecutionRecord.accepted_checkpoint_id)
                    .where(
                        AgentTurnExecutionRecord.workspace_id == workspace_id,
                        AgentTurnExecutionRecord.graph_run_id == graph_run_id,
                        AgentTurnExecutionRecord.accepted_checkpoint_id.is_not(None),
                    )
                    .order_by(
                        AgentTurnExecutionRecord.input_seq.desc(),
                        AgentTurnExecutionRecord.created_at.desc(),
                    )
                    .limit(1)
                )
                if historical is not None:
                    accepted = historical
                else:
                    pending = session.scalar(
                        select(AgentPendingInputRecord)
                        .where(
                            AgentPendingInputRecord.workspace_id == workspace_id,
                            AgentPendingInputRecord.graph_run_id == graph_run_id,
                            AgentPendingInputRecord.accepted_checkpoint_id.is_not(None),
                        )
                        .order_by(AgentPendingInputRecord.created_at.desc())
                        .limit(1)
                    )
                    if pending is not None:
                        accepted = pending.accepted_checkpoint_id

            if accepted is not None and saver is not None:
                locator = CheckpointLocator(
                    execution.checkpoint_thread_id,
                    execution.checkpoint_ns,
                    accepted,
                )
                if saver.get_tuple(locator.as_config()) is None:
                    raise TurnExecutionError("accepted checkpoint head is missing")

            # Lock the Workspace row only after the execution row, preserving
            # the canonical execution -> workspace order.
            workspace = session.scalar(
                select(WorkspaceRecord)
                .where(WorkspaceRecord.id == workspace_id)
                .with_for_update()
            )
            if workspace is None:  # pragma: no cover - FK prevents this
                raise UnknownWorkspaceError(workspace_token)
            lock.require_thread(workspace.agent_thread_id)
            workspace.lease_fence += 1
            new_fence = workspace.lease_fence
            execution.attempt += 1
            execution.lease_fence = new_fence
            execution.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            if accepted is not None and execution.accepted_checkpoint_id is None:
                execution.accepted_checkpoint_id = accepted
            session.flush()

        return RecoveryPlan(
            execution_id=execution.id,
            workspace_id=workspace_id,
            agent_thread_id=workspace.agent_thread_id,
            graph_run_id=graph_run_id,
            checkpoint_thread_id=execution.checkpoint_thread_id,
            input_seq=input_seq,
            input_turn_id=execution.input_turn_id,
            input_event_id=execution.input_event_id,
            attempt=execution.attempt,
            lease_fence=new_fence,
            lease_expires_at=execution.lease_expires_at,
            source="checkpoint" if accepted is not None else "input_event",
            accepted_checkpoint_id=accepted,
        )

    # ------------------------------------------------------------------
    # Complete
    # ------------------------------------------------------------------
    def complete_turn(
        self,
        handle: TurnExecutionHandle,
        *,
        workspace_token: str,
        terminal_event_id: int,
        lock: AdvisoryLockHandle,
        status: Literal[
            "completed",
            "recoverable_error",
            "interrupted",
        ] = "completed",
    ) -> None:
        """Release the lease and terminalize the exact active execution.

        The terminal write must happen on the same advisory-lock owning Session
        so the lock continuously covers takeover, graph run, head promotion and
        finalize.
        """
        session = lock.session
        with session.begin():
            execution = session.scalar(
                select(AgentTurnExecutionRecord)
                .where(
                    AgentTurnExecutionRecord.id == handle.execution_id,
                    AgentTurnExecutionRecord.workspace_id == handle.workspace_id,
                    AgentTurnExecutionRecord.graph_run_id == handle.graph_run_id,
                    AgentTurnExecutionRecord.input_seq == handle.input_seq,
                    AgentTurnExecutionRecord.input_turn_id == handle.input_turn_id,
                    AgentTurnExecutionRecord.actor_id == handle.actor_id,
                    AgentTurnExecutionRecord.auth_session_ref
                    == handle.auth_session_ref,
                    AgentTurnExecutionRecord.lease_fence == handle.lease_fence,
                    AgentTurnExecutionRecord.status == "running",
                )
                .with_for_update()
            )
            if execution is None:
                raise StaleTurnFenceError("execution is not owned by this handle")
            now = datetime.now(UTC)
            if execution.lease_expires_at is None or execution.lease_expires_at <= now:
                raise StaleTurnFenceError("execution lease has expired")
            workspace = session.scalar(
                select(WorkspaceRecord)
                .where(
                    WorkspaceRecord.id == handle.workspace_id,
                    WorkspaceRecord.token_hash == hash_workspace_token(workspace_token),
                    WorkspaceRecord.actor_id == handle.actor_id,
                    WorkspaceRecord.lease_fence == handle.lease_fence,
                )
                .with_for_update()
            )
            if workspace is None:
                raise StaleTurnFenceError("workspace fence does not match execution")
            lock.require_thread(workspace.agent_thread_id)
            execution.status = status
            execution.lease_expires_at = None
            execution.terminal_event_id = terminal_event_id
            execution.updated_at = utc_now()
            session.flush()

    def complete_turn_with_event(
        self,
        handle: TurnExecutionHandle,
        *,
        workspace_token: str,
        lock: AdvisoryLockHandle,
        payload: dict[str, object],
        event_type: Literal["message.completed", "error.recoverable", "turn.interrupted"],
        status: Literal[
            "completed",
            "recoverable_error",
            "interrupted",
        ] = "completed",
    ) -> int:
        """Create the terminal event and terminalize execution in one fenced transaction.

        This is the only finalize path that guarantees a stale owner cannot
        publish a visible terminal event: the execution fence is validated
        before the event row is added, and both the event and the execution
        terminal reference commit together.
        """
        from accesspilot.events import validate_event_payload

        safe_payload = validate_event_payload(event_type, payload)
        session = lock.session
        with session.begin():
            execution = session.scalar(
                select(AgentTurnExecutionRecord)
                .where(
                    AgentTurnExecutionRecord.id == handle.execution_id,
                    AgentTurnExecutionRecord.workspace_id == handle.workspace_id,
                    AgentTurnExecutionRecord.graph_run_id == handle.graph_run_id,
                    AgentTurnExecutionRecord.input_seq == handle.input_seq,
                    AgentTurnExecutionRecord.input_turn_id == handle.input_turn_id,
                    AgentTurnExecutionRecord.actor_id == handle.actor_id,
                    AgentTurnExecutionRecord.auth_session_ref
                    == handle.auth_session_ref,
                    AgentTurnExecutionRecord.lease_fence == handle.lease_fence,
                    AgentTurnExecutionRecord.status == "running",
                )
                .with_for_update()
            )
            if execution is None:
                raise StaleTurnFenceError("execution is not owned by this handle")
            now = datetime.now(UTC)
            if execution.lease_expires_at is None or execution.lease_expires_at <= now:
                raise StaleTurnFenceError("execution lease has expired")
            workspace = session.scalar(
                select(WorkspaceRecord)
                .where(
                    WorkspaceRecord.id == handle.workspace_id,
                    WorkspaceRecord.token_hash == hash_workspace_token(workspace_token),
                    WorkspaceRecord.actor_id == handle.actor_id,
                    WorkspaceRecord.lease_fence == handle.lease_fence,
                )
                .with_for_update()
            )
            if workspace is None:
                raise StaleTurnFenceError("workspace fence does not match execution")
            lock.require_thread(workspace.agent_thread_id)
            terminal = WorkspaceEventRecord(
                workspace_id=handle.workspace_id,
                event_type=event_type,
                payload=safe_payload,
            )
            session.add(terminal)
            session.flush()
            execution.status = status
            execution.lease_expires_at = None
            execution.terminal_event_id = terminal.id
            execution.updated_at = utc_now()
            session.flush()
            return terminal.id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_auth_session(
        session: Session,
        *,
        workspace_id: UUID,
        auth_session_ref: UUID,
        actor_id: str,
        now: datetime,
    ) -> None:
        auth = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.id == auth_session_ref,
                AuthSessionRecord.workspace_id == workspace_id,
                AuthSessionRecord.employee_id == actor_id,
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > now,
            )
        )
        if auth is None:
            raise TurnExecutionError("auth session is invalid or expired")
