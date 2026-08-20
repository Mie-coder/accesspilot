"""T38 isolated JSON adapter for the real production LangGraph.

The adapter composes the T33--T37 application primitives.  It deliberately
does not resolve the product engine or mutate ``Workspace.flow_version``:
callers must inject it explicitly for the isolated JSON gate until T40 opens
the shared JSON/SSE flow-2 canary.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.advisory_lock import AdvisoryLockHandle
from accesspilot.agent.checkpoint import (
    CandidateCheckpointRejected,
    CheckpointUnavailableError,
    ExactCheckpointRequired,
    FencedPostgresSaverAdapter,
    SaverLike,
    ServerExecutionContext,
)
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.engine_binding import (
    EngineBindingConflictError,
    WorkspaceEngineResolver,
)
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphOutput,
    GraphRuntimeContext,
    ProductionGraph,
    build_production_graph,
    route_graph_input,
)
from accesspilot.agent.routing import DeterministicIntentRouter, IntentRouter
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.agent.trace import LANGGRAPH_GRAPH_VERSION
from accesspilot.agent.turn_execution import (
    RecoveryPlan,
    StaleTurnFenceError,
    TurnExecutionError,
    TurnExecutionHandle,
    TurnExecutionService,
    TurnInProgressError,
    TurnLeaseActiveError,
    TurnLockUnavailableError,
    TurnRecoveryInProgressError,
)
from accesspilot.agent.turn_runner import FencedGraphTurnRunner
from accesspilot.auth import InvalidAuthSessionError, load_auth_context
from accesspilot.conversation import (
    ConversationConflictError,
    ConversationInputError,
    ConversationRunResult,
    ConversationTurn,
    ConversationUnavailableError,
    TerminalEventType,
    _explicit_confirmation_from_text,
    normalized_outcome,
)
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.events import ModelQuotaExceededError, get_model_quota
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService

_CONFLICT_MESSAGES: dict[str, str] = {
    "TURN_IN_PROGRESS": "当前 Workspace 已有进行中的对话轮。",
    "TURN_RECOVERY_IN_PROGRESS": "上一轮对话正在恢复，请稍后重试。",
    "TURN_LOCK_UNAVAILABLE": "当前 Workspace 已有对话执行者。",
    "TURN_BINDING_CONFLICT": "对话恢复状态已变化，请刷新后重试。",
    "ENGINE_BINDING_CONFLICT": "Workspace 引擎绑定状态不一致。",
}


@dataclass(frozen=True)
class LangGraphSseAdmission:
    """One fenced HTTP input accepted before its SSE response begins."""

    handle: TurnExecutionHandle
    workspace_token: str
    safe_content: str
    principal: object
    pending_input_id: UUID
    is_resume: bool
    started_event_id: int

    @property
    def turn_id(self) -> str:
        return self.handle.input_turn_id


class LangGraphConversationOrchestrator:
    """Run one JSON HTTP turn through the production compiled graph."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_service: WorkspaceService,
        model: object,
        checkpoint_saver: SaverLike,
        policy_service: PolicyService | None = None,
        router: IntentRouter | None = None,
        lease_seconds: int = 300,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_service = workspace_service
        self._model = model
        self._checkpoint_saver = checkpoint_saver
        self._policy_service = policy_service or PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        )
        self._router = router or DeterministicIntentRouter()
        self._turn_service = TurnExecutionService(
            session_factory,
            lease_seconds=lease_seconds,
        )
        self._heartbeat_interval = heartbeat_interval

    def transport_binding(self, *, workspace_token: str) -> dict[str, object]:
        """Return the persisted binding used by the first SSE frame."""

        with self._session_factory() as session:
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
                )
            )
            if workspace is None:
                raise TurnExecutionError("workspace is unavailable")
            return {
                "orchestrator": "langgraph",
                "flow_version": workspace.flow_version,
                "graph_version": LANGGRAPH_GRAPH_VERSION,
            }

    def transport_turn_id(self, *, workspace_token: str, proposed: str) -> str:
        """Never reuse a running turn outside fenced SSE admission."""

        del workspace_token
        return proposed

    def admit_sse(
        self,
        *,
        workspace_token: str,
        content: str,
        proposed_turn_id: str,
        auth_session_id: str | None,
    ) -> LangGraphSseAdmission:
        """Atomically accept an SSE input before any response frame is sent."""

        safe_content = content.strip()
        if not safe_content:
            raise ConversationInputError("消息不能为空")
        try:
            auth = load_auth_context(
                self._session_factory,
                token=workspace_token,
            )
            if auth_session_id is None or str(auth.session_id) != auth_session_id:
                raise ConversationConflictError(
                    "TURN_BINDING_CONFLICT",
                    _CONFLICT_MESSAGES["TURN_BINDING_CONFLICT"],
                )
            running, pending = self._live_execution_facts(auth.workspace_id)
            if running is not None:
                return self._admit_recovery_sse(
                    workspace_token=workspace_token,
                    safe_content=safe_content,
                    auth_session_ref=auth.session_id,
                    actor_id=auth.principal.employee_id,
                    principal=auth.principal,
                    running=running,
                    pending=pending,
                )
            if pending is not None:
                return self._admit_resume_sse(
                    workspace_token=workspace_token,
                    safe_content=safe_content,
                    auth_session_ref=auth.session_id,
                    actor_id=auth.principal.employee_id,
                    principal=auth.principal,
                    pending=pending,
                    input_turn_id=proposed_turn_id,
                )
            handle = self._turn_service.begin_input(
                workspace_token=workspace_token,
                auth_session_ref=auth.session_id,
                actor_id=auth.principal.employee_id,
                safe_user_text=safe_content,
                input_turn_id=proposed_turn_id,
            )
            return self._sse_admission(
                handle=handle,
                workspace_token=workspace_token,
                safe_content=safe_content,
                principal=auth.principal,
                pending_input_id=uuid4(),
                is_resume=False,
            )
        except ConversationInputError:
            raise
        except ConversationConflictError:
            raise
        except ModelQuotaExceededError:
            raise
        except TurnInProgressError as error:
            raise self._conflict("TURN_IN_PROGRESS") from error
        except TurnRecoveryInProgressError as error:
            raise self._conflict("TURN_RECOVERY_IN_PROGRESS") from error
        except TurnLockUnavailableError as error:
            raise self._conflict("TURN_LOCK_UNAVAILABLE") from error
        except (
            CandidateCheckpointRejected,
            ExactCheckpointRequired,
            StaleTurnFenceError,
            TurnExecutionError,
            TurnLeaseActiveError,
        ) as error:
            raise self._conflict("TURN_BINDING_CONFLICT") from error
        except InvalidAuthSessionError as error:
            raise self._conflict("TURN_BINDING_CONFLICT") from error
        except CheckpointUnavailableError as error:
            raise ConversationUnavailableError() from error
        except Exception as error:
            raise ConversationUnavailableError() from error

    def prepare_admitted(
        self,
        *,
        admission: LangGraphSseAdmission,
    ) -> ConversationRunResult:
        """Execute exactly the input already accepted by ``admit_sse``."""

        with self._turn_service.advisory_lock(
            admission.handle.agent_thread_id
        ) as lock:
            result = self._execute(
                handle=admission.handle,
                workspace_token=admission.workspace_token,
                safe_content=admission.safe_content,
                principal=admission.principal,
                lock=lock,
                pending_input_id=admission.pending_input_id,
                is_resume=admission.is_resume,
                defer_terminal=True,
            )
        return cast(ConversationRunResult, result)

    def prepare(
        self,
        *,
        workspace_token: str,
        content: str,
        turn_id: str,
        auth_session_id: str | None,
    ) -> ConversationRunResult:
        """Execute the graph but defer its fenced terminal to the SSE owner."""

        result = self._run(
            workspace_token=workspace_token,
            content=content,
            auth_session_id=auth_session_id,
            input_turn_id=turn_id,
            defer_terminal=True,
        )
        return cast(ConversationRunResult, result)

    def handle(
        self,
        *,
        workspace_token: str,
        content: str,
        auth_session_id: str | None,
    ) -> ConversationTurn:
        """Accept, execute and atomically finalize one real JSON graph turn."""

        result = self._run(
            workspace_token=workspace_token,
            content=content,
            auth_session_id=auth_session_id,
            input_turn_id=None,
            defer_terminal=False,
        )
        return cast(ConversationTurn, result)

    def _run(
        self,
        *,
        workspace_token: str,
        content: str,
        auth_session_id: str | None,
        input_turn_id: str | None,
        defer_terminal: bool,
    ) -> ConversationTurn | ConversationRunResult:
        """Shared JSON/SSE graph execution; only finalization timing differs."""

        safe_content = content.strip()
        if not safe_content:
            raise ConversationInputError("消息不能为空")
        try:
            auth = load_auth_context(
                self._session_factory,
                token=workspace_token,
            )
            if auth_session_id is None or str(auth.session_id) != auth_session_id:
                raise ConversationConflictError(
                    "TURN_BINDING_CONFLICT",
                    _CONFLICT_MESSAGES["TURN_BINDING_CONFLICT"],
                )
            running, pending = self._live_execution_facts(auth.workspace_id)
            if running is not None:
                return self._recover_or_reject(
                    workspace_token=workspace_token,
                    safe_content=safe_content,
                    auth_session_ref=auth.session_id,
                    actor_id=auth.principal.employee_id,
                    principal=auth.principal,
                    running=running,
                    pending=pending,
                    defer_terminal=defer_terminal,
                )
            if pending is not None:
                return self._run_resume(
                    workspace_token=workspace_token,
                    safe_content=safe_content,
                    auth_session_ref=auth.session_id,
                    actor_id=auth.principal.employee_id,
                    principal=auth.principal,
                    pending=pending,
                    input_turn_id=input_turn_id,
                    defer_terminal=defer_terminal,
                )
            handle = self._turn_service.begin_input(
                workspace_token=workspace_token,
                auth_session_ref=auth.session_id,
                actor_id=auth.principal.employee_id,
                safe_user_text=safe_content,
                input_turn_id=input_turn_id,
            )
            with self._turn_service.advisory_lock(handle.agent_thread_id) as lock:
                return self._execute(
                    handle=handle,
                    workspace_token=workspace_token,
                    safe_content=safe_content,
                    principal=auth.principal,
                    lock=lock,
                    pending_input_id=uuid4(),
                    is_resume=False,
                    defer_terminal=defer_terminal,
                )
        except ConversationInputError:
            raise
        except ConversationConflictError:
            raise
        except ModelQuotaExceededError:
            raise
        except TurnInProgressError as error:
            raise self._conflict("TURN_IN_PROGRESS") from error
        except TurnRecoveryInProgressError as error:
            raise self._conflict("TURN_RECOVERY_IN_PROGRESS") from error
        except TurnLockUnavailableError as error:
            raise self._conflict("TURN_LOCK_UNAVAILABLE") from error
        except (
            CandidateCheckpointRejected,
            ExactCheckpointRequired,
            StaleTurnFenceError,
            TurnExecutionError,
            TurnLeaseActiveError,
        ) as error:
            raise self._conflict("TURN_BINDING_CONFLICT") from error
        except InvalidAuthSessionError as error:
            raise self._conflict("TURN_BINDING_CONFLICT") from error
        except CheckpointUnavailableError as error:
            raise ConversationUnavailableError() from error
        except Exception as error:
            # The HTTP boundary receives one safe code/message only.  The
            # execution remains running if the failure happened after input
            # acceptance, so the T37 takeover path can recover it.
            raise ConversationUnavailableError() from error

    @staticmethod
    def _conflict(code: str) -> ConversationConflictError:
        return ConversationConflictError(code, _CONFLICT_MESSAGES[code])

    def _live_execution_facts(
        self,
        workspace_id: UUID,
    ) -> tuple[AgentTurnExecutionRecord | None, AgentPendingInputRecord | None]:
        with self._session_factory() as session:
            running = session.scalar(
                select(AgentTurnExecutionRecord)
                .where(
                    AgentTurnExecutionRecord.workspace_id == workspace_id,
                    AgentTurnExecutionRecord.status == "running",
                )
                .limit(1)
            )
            pending = session.scalar(
                select(AgentPendingInputRecord)
                .where(
                    AgentPendingInputRecord.workspace_id == workspace_id,
                    AgentPendingInputRecord.status.in_(("active", "resuming")),
                )
                .limit(1)
            )
            if running is not None:
                session.expunge(running)
            if pending is not None:
                session.expunge(pending)
            return running, pending

    def _admit_resume_sse(
        self,
        *,
        workspace_token: str,
        safe_content: str,
        auth_session_ref: UUID,
        actor_id: str,
        principal: object,
        pending: AgentPendingInputRecord,
        input_turn_id: str,
    ) -> LangGraphSseAdmission:
        if pending.status != "active":
            raise self._conflict("TURN_RECOVERY_IN_PROGRESS")
        self._preflight_resume_quota(
            workspace_token=workspace_token,
            safe_content=safe_content,
        )
        with self._turn_service.advisory_lock(pending.agent_thread_id) as lock:
            handle = self._turn_service.begin_resume(
                workspace_token=workspace_token,
                auth_session_ref=auth_session_ref,
                actor_id=actor_id,
                safe_user_text=safe_content,
                pending_input_id=pending.pending_input_id,
                lock=lock,
                saver=self._checkpoint_saver,
                input_turn_id=input_turn_id,
            )
        return self._sse_admission(
            handle=handle,
            workspace_token=workspace_token,
            safe_content=safe_content,
            principal=principal,
            pending_input_id=pending.pending_input_id,
            is_resume=True,
        )

    def _admit_recovery_sse(
        self,
        *,
        workspace_token: str,
        safe_content: str,
        auth_session_ref: UUID,
        actor_id: str,
        principal: object,
        running: AgentTurnExecutionRecord,
        pending: AgentPendingInputRecord | None,
    ) -> LangGraphSseAdmission:
        now = datetime.now(UTC)
        if running.lease_expires_at is None or running.lease_expires_at > now:
            raise self._conflict("TURN_IN_PROGRESS")
        persisted_content = self._safe_input_fact(running.input_event_id)
        if redact_sensitive_content(safe_content) != persisted_content:
            raise self._conflict("TURN_RECOVERY_IN_PROGRESS")
        with self._workspace_lock(running.workspace_id) as agent_thread_id:
            with self._turn_service.advisory_lock(agent_thread_id) as lock:
                plan = self._turn_service.takeover(
                    workspace_token=workspace_token,
                    graph_run_id=running.graph_run_id,
                    input_seq=running.input_seq,
                    auth_session_ref=auth_session_ref,
                    actor_id=actor_id,
                    lock=lock,
                    saver=self._checkpoint_saver,
                )
                handle = self._handle_from_plan(
                    plan,
                    actor_id=actor_id,
                    auth_session_ref=auth_session_ref,
                )
        resumed_pending = (
            pending
            if pending is not None
            and pending.graph_run_id == plan.graph_run_id
            and pending.resume_input_seq == plan.input_seq
            else None
        )
        return self._sse_admission(
            handle=handle,
            workspace_token=workspace_token,
            safe_content=persisted_content,
            principal=principal,
            pending_input_id=(
                resumed_pending.pending_input_id
                if resumed_pending is not None
                else uuid4()
            ),
            is_resume=resumed_pending is not None,
        )

    def _sse_admission(
        self,
        *,
        handle: TurnExecutionHandle,
        workspace_token: str,
        safe_content: str,
        principal: object,
        pending_input_id: UUID,
        is_resume: bool,
    ) -> LangGraphSseAdmission:
        with self._session_factory() as session:
            started_event_id = session.scalar(
                select(WorkspaceEventRecord.id).where(
                    WorkspaceEventRecord.workspace_id == handle.workspace_id,
                    WorkspaceEventRecord.event_type == "turn.started",
                    WorkspaceEventRecord.payload["turn_id"].astext
                    == handle.input_turn_id,
                )
            )
        if started_event_id is None:
            raise TurnExecutionError("admitted turn.started fact is unavailable")
        return LangGraphSseAdmission(
            handle=handle,
            workspace_token=workspace_token,
            safe_content=safe_content,
            principal=principal,
            pending_input_id=pending_input_id,
            is_resume=is_resume,
            started_event_id=started_event_id,
        )

    def _run_resume(
        self,
        *,
        workspace_token: str,
        safe_content: str,
        auth_session_ref: UUID,
        actor_id: str,
        principal: object,
        pending: AgentPendingInputRecord,
        input_turn_id: str | None,
        defer_terminal: bool,
    ) -> ConversationTurn | ConversationRunResult:
        if pending.status != "active":
            raise self._conflict("TURN_RECOVERY_IN_PROGRESS")
        self._preflight_resume_quota(
            workspace_token=workspace_token,
            safe_content=safe_content,
        )
        with self._turn_service.advisory_lock(pending.agent_thread_id) as lock:
            handle = self._turn_service.begin_resume(
                workspace_token=workspace_token,
                auth_session_ref=auth_session_ref,
                actor_id=actor_id,
                safe_user_text=safe_content,
                pending_input_id=pending.pending_input_id,
                lock=lock,
                saver=self._checkpoint_saver,
                input_turn_id=input_turn_id,
            )
            return self._execute(
                handle=handle,
                workspace_token=workspace_token,
                safe_content=safe_content,
                principal=principal,
                lock=lock,
                pending_input_id=pending.pending_input_id,
                is_resume=True,
                defer_terminal=defer_terminal,
            )

    def _preflight_resume_quota(
        self,
        *,
        workspace_token: str,
        safe_content: str,
    ) -> None:
        """Reject model-bound resumes before consuming their exact interrupt.

        A dynamic interrupt's accepted checkpoint cannot be re-used for a
        different HTTP input after LangGraph has persisted resume task writes.
        When the local quota is already exhausted, reject a request-edit
        resume before ``begin_resume`` so the active pending/head remain
        untouched.  Read-only and explicit-confirm inputs still enter the
        graph because they do not consume model quota.
        """
        if _explicit_confirmation_from_text(safe_content) is True:
            return
        route = route_graph_input(
            safe_content,
            router=self._router,
            numeric_cursor_active=True,
        )
        if route.selected_route != "request_access":
            return
        with self._session_factory() as session:
            quota = get_model_quota(session, workspace_token=workspace_token)
        if quota.remaining <= 0:
            raise ModelQuotaExceededError("模型调用额度已用尽")

    def _recover_or_reject(
        self,
        *,
        workspace_token: str,
        safe_content: str,
        auth_session_ref: UUID,
        actor_id: str,
        principal: object,
        running: AgentTurnExecutionRecord,
        pending: AgentPendingInputRecord | None,
        defer_terminal: bool,
    ) -> ConversationTurn | ConversationRunResult:
        if running.lease_expires_at is None or running.lease_expires_at > datetime.now(UTC):
            raise self._conflict("TURN_IN_PROGRESS")
        with self._workspace_lock(running.workspace_id) as agent_thread_id:
            with self._turn_service.advisory_lock(agent_thread_id) as lock:
                plan = self._turn_service.takeover(
                    workspace_token=workspace_token,
                    graph_run_id=running.graph_run_id,
                    input_seq=running.input_seq,
                    auth_session_ref=auth_session_ref,
                    actor_id=actor_id,
                    lock=lock,
                    saver=self._checkpoint_saver,
                )
                recovered = self._handle_from_plan(
                    plan,
                    actor_id=actor_id,
                    auth_session_ref=auth_session_ref,
                )
                persisted_content = self._safe_input_fact(plan.input_event_id)
                resumed_pending = (
                    pending
                    if pending is not None
                    and pending.graph_run_id == plan.graph_run_id
                    and pending.resume_input_seq == plan.input_seq
                    else None
                )
                result = self._execute(
                    handle=recovered,
                    workspace_token=workspace_token,
                    safe_content=persisted_content,
                    principal=principal,
                    lock=lock,
                    pending_input_id=(
                        resumed_pending.pending_input_id
                        if resumed_pending is not None
                        else uuid4()
                    ),
                    is_resume=resumed_pending is not None,
                    defer_terminal=defer_terminal,
                )
        if safe_content != persisted_content:
            # This request was a genuinely new input, not a retry of the
            # accepted crashed input.  Recovery completed, but the new text was
            # never accepted or persisted; a 409 tells the client to retry it.
            raise self._conflict("TURN_RECOVERY_IN_PROGRESS")
        return result

    @contextmanager
    def _workspace_lock(self, workspace_id: UUID) -> Iterator[UUID]:
        """Read the immutable server thread before acquiring its advisory lock."""

        with self._session_factory() as session:
            workspace = session.get(WorkspaceRecord, workspace_id)
            if workspace is None:
                raise TurnExecutionError("workspace is unavailable")
            agent_thread_id = workspace.agent_thread_id
        yield agent_thread_id

    def _execute(
        self,
        *,
        handle: TurnExecutionHandle,
        workspace_token: str,
        safe_content: str,
        principal: object,
        lock: AdvisoryLockHandle,
        pending_input_id: UUID,
        is_resume: bool,
        defer_terminal: bool,
    ) -> ConversationTurn | ConversationRunResult:
        context = self._server_context(handle.execution_id)
        invocation = FencedPostgresSaverAdapter(
            self._checkpoint_saver
        ).for_execution(context)
        # FencedSaverInvocation gains BaseCheckpointSaver as a nominal runtime
        # base after strict-msgpack setup; the dynamic base is intentionally
        # invisible to static type checkers.
        graph = build_production_graph(checkpointer=cast(Any, invocation))
        config = context.accepted_locator.as_config() if context.accepted_locator else {
            "configurable": {
                "thread_id": context.checkpoint_thread_id,
                "checkpoint_ns": context.checkpoint_ns,
            }
        }
        runtime_context = self._runtime_context(
            handle=handle,
            workspace_token=workspace_token,
            principal=principal,
            pending_input_id=pending_input_id,
        )
        graph_input: GraphInput | Command[Any]
        if is_resume:
            graph_input = Command(
                resume={
                    "decision": (
                        "confirm"
                        if _explicit_confirmation_from_text(safe_content) is True
                        else "route_new_input"
                    ),
                    "safe_user_text": safe_content,
                }
            )
        else:
            graph_input = GraphInput(
                schema_version=1,
                flow_version=2,
                workspace_ref=handle.workspace_id,
                graph_run_id=handle.graph_run_id,
                input_seq=handle.input_seq,
                input_turn_id=handle.input_turn_id,
                safe_user_text=safe_content,
                input_kind="new_input",
            )
        runner = FencedGraphTurnRunner(
            graph,
            self._turn_service,
            heartbeat_interval=self._heartbeat_interval,
        )
        try:
            output = runner.run(
                handle,
                graph_input,
                runtime_context,
                lock,
                config,
            )
        except ModelQuotaExceededError:
            quota_payload: dict[str, object] = {
                "turn_id": handle.input_turn_id,
                "code": "MODEL_QUOTA_EXHAUSTED",
                "message": "模型调用额度已用尽，当前为只读回放模式",
            }
            self._turn_service.complete_turn_with_event(
                handle,
                workspace_token=workspace_token,
                lock=lock,
                event_type="error.recoverable",
                payload=quota_payload,
            )
            raise
        except ConfirmationInterruptRaised as stopped:
            verified = self._verify_interrupt(invocation, graph)
            revision = stopped.payload.get("draft_revision")
            payload_pending = stopped.payload.get("pending_input_id")
            if type(revision) is not int or payload_pending != str(pending_input_id):
                raise CandidateCheckpointRejected(
                    "interrupt payload does not match the accepted execution"
                ) from stopped
            self._turn_service.finalize_interrupt(
                handle,
                workspace_token=workspace_token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                draft_revision=revision,
                previous_pending_input_id=pending_input_id if is_resume else None,
            ) if not defer_terminal else None
            turn = self._interrupt_turn(
                workspace_token=workspace_token,
                message=str(stopped.payload["summary"]),
                draft_revision=revision,
            )
            if not defer_terminal:
                return turn
            return ConversationRunResult(
                turn=turn,
                terminal_finalizer=lambda event_type, payload: (
                    self._finalize_deferred_terminal(
                        handle=handle,
                        workspace_token=workspace_token,
                        verified=verified,
                        pending_input_id=pending_input_id,
                        is_resume=is_resume,
                        is_interrupt=True,
                        draft_revision=revision,
                        event_type=event_type,
                        payload=payload,
                    )
                ),
            )
        turn = self._conversation_turn(GraphOutput.model_validate(output))
        verified = self._verify_end(invocation, graph)
        event_type: Literal["message.completed", "error.recoverable"] = (
            "error.recoverable"
            if turn.business_status == "recoverable_error"
            else "message.completed"
        )
        terminal_payload = self._terminal_payload(handle, turn, event_type=event_type)
        if defer_terminal:
            return ConversationRunResult(
                turn=turn,
                terminal_finalizer=lambda terminal_type, terminal_payload: (
                    self._finalize_deferred_terminal(
                        handle=handle,
                        workspace_token=workspace_token,
                        verified=verified,
                        pending_input_id=pending_input_id,
                        is_resume=is_resume,
                        is_interrupt=False,
                        draft_revision=turn.draft_revision,
                        event_type=terminal_type,
                        payload=terminal_payload,
                    )
                ),
            )
        if is_resume:
            self._turn_service.finalize_resume_outcome(
                handle,
                workspace_token=workspace_token,
                lock=lock,
                verified=verified,
                pending_input_id=pending_input_id,
                event_type=event_type,
                payload=terminal_payload,
            )
        else:
            self._turn_service.finalize_graph_turn_with_event(
                handle,
                workspace_token=workspace_token,
                lock=lock,
                verified=verified,
                event_type=event_type,
                payload=terminal_payload,
            )
        return turn

    def _finalize_deferred_terminal(
        self,
        *,
        handle: TurnExecutionHandle,
        workspace_token: str,
        verified: Any,
        pending_input_id: UUID,
        is_resume: bool,
        is_interrupt: bool,
        draft_revision: int,
        event_type: TerminalEventType,
        payload: dict[str, object],
    ) -> int | None:
        """Finalize only after the synchronous graph worker has returned.

        The original advisory-lock context has ended, but the execution lease
        remains live and prevents takeover.  Reacquiring the same server-side
        thread lock closes the candidate and terminal atomically.  A competing
        completion/disconnect finalizer observes the already-written terminal
        instead of creating a second one.
        """

        try:
            with self._turn_service.advisory_lock(handle.agent_thread_id) as lock:
                if is_interrupt:
                    return self._turn_service.finalize_interrupt(
                        handle,
                        workspace_token=workspace_token,
                        lock=lock,
                        verified=verified,
                        pending_input_id=pending_input_id,
                        draft_revision=draft_revision,
                        previous_pending_input_id=(
                            pending_input_id if is_resume else None
                        ),
                        terminal_event_type=event_type,
                        terminal_payload=payload,
                    )
                if is_resume:
                    return self._turn_service.finalize_resume_outcome(
                        handle,
                        workspace_token=workspace_token,
                        lock=lock,
                        verified=verified,
                        pending_input_id=pending_input_id,
                        event_type=event_type,
                        payload=payload,
                    )
                return self._turn_service.finalize_graph_turn_with_event(
                    handle,
                    workspace_token=workspace_token,
                    lock=lock,
                    verified=verified,
                    event_type=event_type,
                    payload=payload,
                )
        except StaleTurnFenceError:
            with self._session_factory() as session:
                execution = session.get(
                    AgentTurnExecutionRecord,
                    handle.execution_id,
                )
                if execution is not None and execution.terminal_event_id is not None:
                    return execution.terminal_event_id
            raise

    def _runtime_context(
        self,
        *,
        handle: TurnExecutionHandle,
        workspace_token: str,
        principal: object,
        pending_input_id: UUID,
    ) -> GraphRuntimeContext:
        return {
            "session_factory": self._session_factory,
            "workspace_service": self._workspace_service,
            "policy_service": self._policy_service,
            "structured_reply_model": self._model,
            "intent_router": self._router,
            "principal": principal,
            "current_turn_id": handle.input_turn_id,
            "current_input_seq": handle.input_seq,
            "current_fence": handle.lease_fence,
            "workspace_token": workspace_token,
            "auth_session_id": str(handle.auth_session_ref),
            # Secrets are intentionally unavailable to this adapter.  These
            # context-only placeholders are never serialized into GraphState.
            "cookie": "",
            "csrf_token": "",
            "api_key": "",
            "pending_input_id": str(pending_input_id),
            "trace_enabled": True,
        }

    def _server_context(self, execution_id: UUID) -> ServerExecutionContext:
        with self._session_factory() as session:
            execution = session.get(AgentTurnExecutionRecord, execution_id)
            if execution is None:
                raise TurnExecutionError("execution is unavailable")
            return ServerExecutionContext.from_record(execution)

    def _safe_input_fact(self, event_id: int) -> str:
        with self._session_factory() as session:
            event = session.get(WorkspaceEventRecord, event_id)
            if event is None or event.event_type != "message.user":
                raise TurnExecutionError("safe input fact is unavailable")
            content = event.payload.get("content")
            if not isinstance(content, str) or not content:
                raise TurnExecutionError("safe input fact is unavailable")
            return content

    @staticmethod
    def _handle_from_plan(
        plan: RecoveryPlan,
        *,
        actor_id: str,
        auth_session_ref: UUID,
    ) -> TurnExecutionHandle:
        return TurnExecutionHandle(
            execution_id=plan.execution_id,
            workspace_id=plan.workspace_id,
            agent_thread_id=plan.agent_thread_id,
            graph_run_id=plan.graph_run_id,
            checkpoint_thread_id=plan.checkpoint_thread_id,
            input_seq=plan.input_seq,
            input_turn_id=plan.input_turn_id,
            input_event_id=plan.input_event_id,
            attempt=plan.attempt,
            lease_fence=plan.lease_fence,
            lease_expires_at=plan.lease_expires_at,
            actor_id=actor_id,
            auth_session_ref=auth_session_ref,
            accepted_checkpoint_id=plan.accepted_checkpoint_id,
        )

    @staticmethod
    def _verify_interrupt(invocation: Any, graph: ProductionGraph) -> Any:
        candidate = invocation.candidate
        if candidate is None:
            raise CandidateCheckpointRejected("interrupt candidate is missing")
        return invocation.verify_candidate(
            candidate,
            graph_stopped=True,
            graph_state_reader=graph.compiled.get_state,
            state_validator=lambda state: any(task.interrupts for task in state.tasks),
        )

    @staticmethod
    def _verify_end(invocation: Any, graph: ProductionGraph) -> Any:
        candidate = invocation.candidate
        if candidate is None:
            raise CandidateCheckpointRejected("END candidate is missing")
        return invocation.verify_candidate(
            candidate,
            graph_stopped=True,
            graph_state_reader=graph.compiled.get_state,
            state_validator=lambda state: not state.next,
        )

    def _interrupt_turn(
        self,
        *,
        workspace_token: str,
        message: str,
        draft_revision: int,
    ) -> ConversationTurn:
        workspace = self._workspace_service.peek(workspace_token)
        draft = workspace.draft
        if draft is None:
            raise TurnExecutionError("interrupt draft is unavailable")
        with self._session_factory() as session:
            quota = get_model_quota(session, workspace_token=workspace_token)
        return ConversationTurn(
            assistant_message=message,
            draft=draft,
            missing_fields=draft.missing_fields(),
            phase="awaiting_confirmation",
            business_status="awaiting_confirmation",
            quota=quota,
            intent="request_access",
            security_flagged=False,
            tool_results=[],
            draft_revision=draft_revision,
        )

    @staticmethod
    def _conversation_turn(output: GraphOutput) -> ConversationTurn:
        return ConversationTurn.model_validate(output.model_dump(mode="python"))

    @staticmethod
    def _terminal_payload(
        handle: TurnExecutionHandle,
        turn: ConversationTurn,
        *,
        event_type: str,
    ) -> dict[str, object]:
        outcome = normalized_outcome(turn)
        if event_type == "error.recoverable":
            return {
                "turn_id": handle.input_turn_id,
                "code": turn.error_code or "GRAPH_RECOVERABLE_ERROR",
                "message": turn.assistant_message,
                **outcome,
            }
        return {
            "turn_id": handle.input_turn_id,
            "message_id": f"msg-{uuid4()}",
            "content": turn.assistant_message,
            **outcome,
        }


class WorkspaceBoundConversationOrchestrator:
    """Dispatch both JSON and SSE from one persisted Workspace binding."""

    def __init__(
        self,
        *,
        resolver: WorkspaceEngineResolver,
        legacy: object,
        langgraph: LangGraphConversationOrchestrator | None,
    ) -> None:
        self._resolver = resolver
        self._legacy = legacy
        self._langgraph = langgraph

    def _delegate(self, workspace_token: str) -> object:
        try:
            engine = self._resolver.resolve(workspace_token=workspace_token)
        except EngineBindingConflictError as error:
            raise ConversationConflictError(
                "ENGINE_BINDING_CONFLICT",
                _CONFLICT_MESSAGES["ENGINE_BINDING_CONFLICT"],
            ) from error
        if engine == "legacy":
            return self._legacy
        if self._langgraph is None:
            raise ConversationUnavailableError()
        return self._langgraph

    def handle(
        self,
        *,
        workspace_token: str,
        content: str,
        auth_session_id: str | None,
    ) -> ConversationTurn:
        delegate = self._delegate(workspace_token)
        return delegate.handle(  # type: ignore[attr-defined,no-any-return]
            workspace_token=workspace_token,
            content=content,
            auth_session_id=auth_session_id,
        )

    def prepare(
        self,
        *,
        workspace_token: str,
        content: str,
        turn_id: str,
        auth_session_id: str | None,
    ) -> ConversationRunResult:
        delegate = self._delegate(workspace_token)
        return delegate.prepare(  # type: ignore[attr-defined,no-any-return]
            workspace_token=workspace_token,
            content=content,
            turn_id=turn_id,
            auth_session_id=auth_session_id,
        )

    def admit_sse(
        self,
        *,
        workspace_token: str,
        content: str,
        proposed_turn_id: str,
        auth_session_id: str | None,
    ) -> LangGraphSseAdmission | None:
        delegate = self._delegate(workspace_token)
        admitter = getattr(delegate, "admit_sse", None)
        if admitter is None:
            return None
        return cast(
            LangGraphSseAdmission,
            admitter(
                workspace_token=workspace_token,
                content=content,
                proposed_turn_id=proposed_turn_id,
                auth_session_id=auth_session_id,
            ),
        )

    def prepare_admitted(
        self,
        *,
        admission: LangGraphSseAdmission,
    ) -> ConversationRunResult:
        if self._langgraph is None:
            raise ConversationUnavailableError()
        return self._langgraph.prepare_admitted(admission=admission)

    def transport_binding(self, *, workspace_token: str) -> dict[str, object]:
        delegate = self._delegate(workspace_token)
        metadata = getattr(delegate, "transport_binding", None)
        if metadata is not None:
            return cast(dict[str, object], metadata(workspace_token=workspace_token))
        engine = self._resolver.resolve(workspace_token=workspace_token)
        return {
            "orchestrator": engine,
            "flow_version": 1 if engine == "legacy" else 2,
        }

    def transport_turn_id(self, *, workspace_token: str, proposed: str) -> str:
        delegate = self._delegate(workspace_token)
        resolver = getattr(delegate, "transport_turn_id", None)
        if resolver is None:
            return proposed
        return cast(
            str,
            resolver(workspace_token=workspace_token, proposed=proposed),
        )
