"""Fenced, transaction-aware T32 model quota and draft step operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.identity import confirm_operation_id, operation_id
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentStepExecutionRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceRecord,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.domain.models import RequestDraft
from accesspilot.events import ModelQuota, ModelQuotaExceededError
from accesspilot.workspaces import CursorConflictError, DraftRevisionConflictError


class StepExecutionRejected(RuntimeError):
    """The runtime does not own the exact active execution it claims."""


class StepOperationConflict(RuntimeError):
    """A stable operation identity was reused for different logical facts."""


class AgentStepContext(BaseModel):
    """Canonical server-owned coordinates required by every T32 write."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    graph_run_id: UUID
    input_seq: int = Field(ge=0)
    input_turn_id: str = Field(min_length=1, max_length=120)
    actor_id: str = Field(min_length=1, max_length=30)
    auth_session_ref: UUID
    lease_fence: int = Field(ge=1)


class ModelAttemptOperation(BaseModel):
    """Safe result of reserving or reading one local quota-attempt operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    status: Literal["reserved", "completed"]
    quota: ModelQuota
    result_reference: str | None = None


class CompletedStepOperation(BaseModel):
    """Safe completed local-CAS fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    step_key: str
    committed_revision: int = Field(ge=0)
    replayed: bool


def _quota(workspace: WorkspaceRecord) -> ModelQuota:
    return ModelQuota(
        used=workspace.model_calls_used,
        limit=workspace.model_call_limit,
        remaining=workspace.model_call_limit - workspace.model_calls_used,
        retry_consumed=workspace.model_retry_consumed,
    )


class AgentStepOperationService:
    """Own transactions that must include both the ledger and local mutation."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _validated_context(context: AgentStepContext) -> AgentStepContext:
        try:
            return AgentStepContext.model_validate(context.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            raise StepExecutionRejected("active graph execution binding failed") from None

    # Canonical T32 write-transaction lock order is execution -> step ->
    # workspace. Every mutating method in this service (and the T33 takeover
    # path) must acquire these locks in exactly this order: the execution
    # lock re-checks engine/status/lease/fence/actor/session first, the step
    # lock deduplicates the logical operation, and the workspace row lock
    # re-checks token + actor + current lease_fence before any Cursor or
    # draft mutation commits.
    @staticmethod
    def _lock_execution(
        session: Session,
        context: AgentStepContext,
    ) -> AgentTurnExecutionRecord:
        execution = session.scalar(
            select(AgentTurnExecutionRecord)
            .where(
                AgentTurnExecutionRecord.workspace_id == context.workspace_id,
                AgentTurnExecutionRecord.graph_run_id == context.graph_run_id,
                AgentTurnExecutionRecord.input_seq == context.input_seq,
                AgentTurnExecutionRecord.input_turn_id == context.input_turn_id,
                AgentTurnExecutionRecord.actor_id == context.actor_id,
                AgentTurnExecutionRecord.auth_session_ref == context.auth_session_ref,
                AgentTurnExecutionRecord.lease_fence == context.lease_fence,
                AgentTurnExecutionRecord.engine == "langgraph",
                AgentTurnExecutionRecord.status == "running",
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if (
            execution is None
            or execution.lease_expires_at is None
            or execution.lease_expires_at <= now
        ):
            raise StepExecutionRejected("active graph execution binding failed")
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.id == context.auth_session_ref,
                AuthSessionRecord.workspace_id == context.workspace_id,
                AuthSessionRecord.employee_id == context.actor_id,
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > now,
            )
        )
        if auth_session is None:
            raise StepExecutionRejected("active graph execution binding failed")
        return execution

    @staticmethod
    def _lock_workspace(
        session: Session,
        context: AgentStepContext,
        *,
        workspace_token: str,
    ) -> WorkspaceRecord:
        workspace = session.scalar(
            select(WorkspaceRecord)
            .where(
                WorkspaceRecord.id == context.workspace_id,
                WorkspaceRecord.token_hash == hash_workspace_token(workspace_token),
                WorkspaceRecord.actor_id == context.actor_id,
                WorkspaceRecord.lease_fence == context.lease_fence,
            )
            .with_for_update()
        )
        if workspace is None:
            raise StepExecutionRejected("active graph execution binding failed")
        return workspace

    @staticmethod
    def _step_key_for_attempt(attempt: int) -> str:
        if attempt not in {1, 2}:
            raise ValueError("model attempt must be 1 or 2")
        return f"parse_request_patch:quota:{attempt}"

    @staticmethod
    def _operation(
        context: AgentStepContext,
        *,
        step_key: str,
    ) -> str:
        return operation_id(
            workspace_id=context.workspace_id,
            graph_run_id=context.graph_run_id,
            input_seq=context.input_seq,
            step_key=step_key,
        )

    @staticmethod
    def _load_step(
        session: Session,
        context: AgentStepContext,
        *,
        step_key: str,
    ) -> AgentStepExecutionRecord | None:
        operation = AgentStepOperationService._operation(
            context,
            step_key=step_key,
        )
        step = session.scalar(
            select(AgentStepExecutionRecord)
            .where(
                AgentStepExecutionRecord.workspace_id == context.workspace_id,
                AgentStepExecutionRecord.operation_id == operation,
            )
            .with_for_update()
        )
        if step is not None and (
            step.graph_run_id != context.graph_run_id
            or step.input_seq != context.input_seq
            or step.step_key != step_key
        ):
            raise StepOperationConflict("step operation identity does not match")
        return step

    def reserve_model_attempt(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        attempt: int,
    ) -> ModelAttemptOperation:
        """Reserve an attempt and increment its quota in the same transaction."""

        context = self._validated_context(context)
        step_key = self._step_key_for_attempt(attempt)
        operation = self._operation(context, step_key=step_key)
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = self._load_step(session, context, step_key=step_key)
            workspace = self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            if step is None:
                if workspace.model_calls_used >= workspace.model_call_limit:
                    raise ModelQuotaExceededError("模型调用额度已用尽")
                workspace.model_calls_used += 1
                if attempt == 2:
                    workspace.model_retry_consumed += 1
                step = AgentStepExecutionRecord(
                    workspace_id=context.workspace_id,
                    graph_run_id=context.graph_run_id,
                    input_seq=context.input_seq,
                    step_key=step_key,
                    operation_id=operation,
                    status="reserved",
                )
                session.add(step)
                session.flush()
            return ModelAttemptOperation(
                operation_id=operation,
                status=step.status,
                quota=_quota(workspace),
                result_reference=step.result_reference,
            )

    def complete_model_attempt(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        attempt: int,
    ) -> ModelAttemptOperation:
        """Record only that local quota accounting completed, never provider output."""

        context = self._validated_context(context)
        safe_reference = "quota_consumed"
        step_key = self._step_key_for_attempt(attempt)
        operation = self._operation(context, step_key=step_key)
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = self._load_step(session, context, step_key=step_key)
            if step is None:
                raise StepOperationConflict("model attempt was not reserved")
            if step.status == "completed":
                if step.result_reference != safe_reference:
                    raise StepOperationConflict("completed model result changed")
            else:
                step.status = "completed"
                step.result_reference = safe_reference
                step.completed_at = datetime.now(UTC)
            workspace = self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            return ModelAttemptOperation(
                operation_id=operation,
                status="completed",
                quota=_quota(workspace),
                result_reference=step.result_reference,
            )

    def completed_step(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        step_key: str,
    ) -> CompletedStepOperation | None:
        """Read a completed operation before interpreting mutable Cursor state."""

        context = self._validated_context(context)
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = self._load_step(session, context, step_key=step_key)
            self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            if step is None or step.status != "completed":
                return None
            if step.committed_revision is None:
                raise StepOperationConflict("completed local step has no revision")
            return CompletedStepOperation(
                operation_id=step.operation_id,
                step_key=step.step_key,
                committed_revision=step.committed_revision,
                replayed=True,
            )

    def persist_draft(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        expected_revision: int,
        draft: RequestDraft,
    ) -> CompletedStepOperation:
        context = self._validated_context(context)
        return self._persist_draft(
            context,
            workspace_token=workspace_token,
            expected_revision=expected_revision,
            draft=draft,
            step_key="persist_draft_cas",
            cursor_expected_field=None,
        )

    def persist_numeric_duration(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        expected_revision: int,
        draft: RequestDraft,
    ) -> CompletedStepOperation:
        context = self._validated_context(context)
        return self._persist_draft(
            context,
            workspace_token=workspace_token,
            expected_revision=expected_revision,
            draft=draft,
            step_key="persist_numeric_duration",
            cursor_expected_field="duration_days",
        )

    def persist_justification_cursor(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        expected_revision: int,
        draft: RequestDraft,
    ) -> CompletedStepOperation:
        context = self._validated_context(context)
        return self._persist_draft(
            context,
            workspace_token=workspace_token,
            expected_revision=expected_revision,
            draft=draft,
            step_key="persist_justification_cursor",
            cursor_expected_field="justification",
        )

    def activate_missing_cursor(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        expected_revision: int,
        expected_field: str,
    ) -> CompletedStepOperation:
        """Project the next missing-field Cursor with a fenced execution binding.

        The Cursor projection is a business write like any other: the
        transaction re-checks the current execution lease/fence, deduplicates
        the stable operation, and only then writes the five Cursor columns
        together with the completed ledger fact. A stale executor, an expired
        lease, a moved draft revision, or a Cursor bound to another session
        all fail closed without any write.
        """

        context = self._validated_context(context)
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if expected_field not in {"entitlement_id", "duration_days", "justification"}:
            raise ValueError("不支持的 Cursor expected_field")
        step_key = "activate_missing_cursor"
        operation = self._operation(context, step_key=step_key)
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = self._load_step(session, context, step_key=step_key)
            workspace = self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            if step is not None:
                if step.status != "completed" or step.committed_revision is None:
                    raise StepOperationConflict("local CAS step is not completed")
                return CompletedStepOperation(
                    operation_id=operation,
                    step_key=step_key,
                    committed_revision=step.committed_revision,
                    replayed=True,
                )
            if workspace.draft_revision != expected_revision:
                raise DraftRevisionConflictError("追问绑定的草稿 revision 已变化")
            if (
                workspace.cursor_expected_field is not None
                and workspace.cursor_auth_session_id != str(context.auth_session_ref)
            ):
                raise DraftRevisionConflictError("追问绑定的草稿 revision 已变化")
            now = datetime.now(UTC)
            workspace.cursor_actor_id = context.actor_id
            workspace.cursor_auth_session_id = str(context.auth_session_ref)
            workspace.cursor_expected_field = expected_field
            workspace.cursor_last_question_kind = expected_field
            workspace.cursor_issued_at = now
            workspace.cursor_consumed_at = None
            committed_revision = workspace.draft_revision
            session.add(
                AgentStepExecutionRecord(
                    workspace_id=context.workspace_id,
                    graph_run_id=context.graph_run_id,
                    input_seq=context.input_seq,
                    step_key=step_key,
                    operation_id=operation,
                    status="completed",
                    committed_revision=committed_revision,
                    completed_at=now,
                )
            )
            session.flush()
            return CompletedStepOperation(
                operation_id=operation,
                step_key=step_key,
                committed_revision=committed_revision,
                replayed=False,
            )

    def confirm_draft(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        pending_input_id: UUID,
        expected_revision: int,
    ) -> CompletedStepOperation:
        """Apply the one confirmation CAS using the stable confirm operation id."""
        context = self._validated_context(context)
        operation = confirm_operation_id(
            workspace_id=context.workspace_id,
            pending_input_id=pending_input_id,
        )
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = session.scalar(
                select(AgentStepExecutionRecord)
                .where(
                    AgentStepExecutionRecord.workspace_id == context.workspace_id,
                    AgentStepExecutionRecord.operation_id == operation,
                )
                .with_for_update()
            )
            workspace = self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            if step is not None:
                if step.status != "completed" or step.committed_revision is None:
                    raise StepOperationConflict("confirmation operation is not completed")
                return CompletedStepOperation(
                    operation_id=operation,
                    step_key="apply_confirmation",
                    committed_revision=step.committed_revision,
                    replayed=True,
                )
            if workspace.draft_revision != expected_revision:
                raise DraftRevisionConflictError("confirmation revision has changed")
            pending = session.scalar(
                select(AgentPendingInputRecord)
                .where(
                    AgentPendingInputRecord.workspace_id == context.workspace_id,
                    AgentPendingInputRecord.pending_input_id == pending_input_id,
                    AgentPendingInputRecord.status.in_(("active", "resuming")),
                )
                .with_for_update()
            )
            if pending is None:
                raise StepExecutionRejected("confirmation pending input is not active")
            draft = dict(workspace.draft) if workspace.draft is not None else {}
            if draft.get("confirmed") is not True:
                draft["confirmed"] = True
                workspace.draft = draft
                workspace.draft_revision += 1
            committed_revision = workspace.draft_revision
            session.add(
                AgentStepExecutionRecord(
                    workspace_id=context.workspace_id,
                    graph_run_id=context.graph_run_id,
                    input_seq=context.input_seq,
                    step_key="apply_confirmation",
                    operation_id=operation,
                    status="completed",
                    committed_revision=committed_revision,
                    completed_at=datetime.now(UTC),
                )
            )
            session.flush()
            return CompletedStepOperation(
                operation_id=operation,
                step_key="apply_confirmation",
                committed_revision=committed_revision,
                replayed=False,
            )

    def completed_confirmation(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        pending_input_id: UUID,
    ) -> CompletedStepOperation | None:
        """Read the stable confirmation fact after a post-CAS process crash.

        The lookup is fenced like every other step read.  It exists separately
        from ``completed_step`` because confirmation identity is derived from
        ``pending_input_id`` rather than ``graph_run_id + input_seq``.
        """

        context = self._validated_context(context)
        operation = confirm_operation_id(
            workspace_id=context.workspace_id,
            pending_input_id=pending_input_id,
        )
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = session.scalar(
                select(AgentStepExecutionRecord)
                .where(
                    AgentStepExecutionRecord.workspace_id == context.workspace_id,
                    AgentStepExecutionRecord.operation_id == operation,
                )
                .with_for_update()
            )
            self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            if step is None:
                return None
            if (
                step.graph_run_id != context.graph_run_id
                or step.step_key != "apply_confirmation"
                or step.status != "completed"
                or step.committed_revision is None
            ):
                raise StepOperationConflict(
                    "completed confirmation operation does not match"
                )
            return CompletedStepOperation(
                operation_id=step.operation_id,
                step_key=step.step_key,
                committed_revision=step.committed_revision,
                replayed=True,
            )

    def _persist_draft(
        self,
        context: AgentStepContext,
        *,
        workspace_token: str,
        expected_revision: int,
        draft: RequestDraft,
        step_key: str,
        cursor_expected_field: Literal["duration_days", "justification"] | None,
    ) -> CompletedStepOperation:
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        safe_draft = draft.model_copy(update={"employee_id": context.actor_id, "confirmed": False})
        operation = self._operation(context, step_key=step_key)
        with self._session_factory() as session, session.begin():
            self._lock_execution(session, context)
            step = self._load_step(session, context, step_key=step_key)
            workspace = self._lock_workspace(
                session,
                context,
                workspace_token=workspace_token,
            )
            if step is not None:
                if step.status != "completed" or step.committed_revision is None:
                    raise StepOperationConflict("local CAS step is not completed")
                return CompletedStepOperation(
                    operation_id=operation,
                    step_key=step_key,
                    committed_revision=step.committed_revision,
                    replayed=True,
                )
            if workspace.draft_revision != expected_revision:
                if cursor_expected_field is not None:
                    raise CursorConflictError("当前 Cursor 已失效或已被消费")
                raise DraftRevisionConflictError("草稿 revision 已发生变化")
            if cursor_expected_field is not None:
                if (
                    workspace.cursor_expected_field != cursor_expected_field
                    or workspace.cursor_consumed_at is not None
                    or workspace.cursor_actor_id != context.actor_id
                    or workspace.cursor_auth_session_id != str(context.auth_session_ref)
                ):
                    raise CursorConflictError("当前 Cursor 已失效或已被消费")
            elif workspace.cursor_expected_field is not None and (
                workspace.cursor_auth_session_id != str(context.auth_session_ref)
            ):
                raise DraftRevisionConflictError("草稿 revision 已发生变化")

            current_draft = (
                RequestDraft.model_validate(workspace.draft)
                if workspace.draft is not None
                else None
            )
            if current_draft != safe_draft:
                workspace.draft = safe_draft.model_dump(mode="json")
                workspace.draft_revision += 1
                if cursor_expected_field is not None:
                    workspace.cursor_consumed_at = datetime.now(UTC)
                else:
                    workspace.cursor_actor_id = None
                    workspace.cursor_auth_session_id = None
                    workspace.cursor_expected_field = None
                    workspace.cursor_last_question_kind = None
                    workspace.cursor_issued_at = None
                    workspace.cursor_consumed_at = None
            committed_revision = workspace.draft_revision
            session.add(
                AgentStepExecutionRecord(
                    workspace_id=context.workspace_id,
                    graph_run_id=context.graph_run_id,
                    input_seq=context.input_seq,
                    step_key=step_key,
                    operation_id=operation,
                    status="completed",
                    committed_revision=committed_revision,
                    completed_at=datetime.now(UTC),
                )
            )
            session.flush()
            return CompletedStepOperation(
                operation_id=operation,
                step_key=step_key,
                committed_revision=committed_revision,
                replayed=False,
            )
