"""T35 workspace sticky engine binding.

One Workspace is bound to exactly one engine for its whole life:

- ``flow_version=1`` binds Legacy, ``flow_version=2`` binds LangGraph;
- the server assigns the flow once, inside the Workspace creation
  transaction, from ``legacy/mixed/langgraph`` + canary configuration.  In
  ``mixed`` the assignment is a stable hash of the server-generated
  ``agent_thread_id``, so a written flow never drifts when configuration
  changes and no client can steer it;
- runtime resolution follows the fixed fact order
  ``running execution.engine -> active/resuming pending.engine -> Workspace
  flow_version``.  Co-present facts must agree; any disagreement is a fixed
  ``409 ENGINE_BINDING_CONFLICT`` and never silently re-routes the Workspace
  to the other engine.

The JSON/SSE entry wiring that consumes this resolution belongs to T38/T40;
until those entry gates exist the mixed canary must stay at 0 (enforced by
``Settings``).
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    WorkspaceRecord,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.workspaces import UnknownWorkspaceError

Engine = Literal["legacy", "langgraph"]
OrchestratorMode = Literal["legacy", "mixed", "langgraph"]
FlowVersion = Literal[1, 2]

_LIVE_PENDING_STATUSES = ("active", "resuming")


class EngineBindingError(RuntimeError):
    """Base class for stable engine binding failures."""

    code = "ENGINE_BINDING_ERROR"


class EngineBindingConflictError(EngineBindingError):
    """Co-present binding facts disagree; never forward or mutate either engine."""

    code = "ENGINE_BINDING_CONFLICT"


def engine_to_flow(engine: Engine) -> FlowVersion:
    if engine == "legacy":
        return 1
    return 2


def flow_to_engine(flow_version: int) -> Engine:
    if flow_version == 1:
        return "legacy"
    if flow_version == 2:
        return "langgraph"
    raise EngineBindingError(f"unsupported flow_version: {flow_version}")


def allocate_flow_version(
    agent_thread_id: UUID,
    *,
    mode: OrchestratorMode,
    canary_percent: int | None,
) -> FlowVersion:
    """Server-side flow assignment for a brand-new Workspace.

    Only the creation transaction may call this; afterwards the written
    ``flow_version`` column is authoritative and never recomputed.  The mixed
    bucket is a pure function of ``agent_thread_id`` (SHA-256), so the same id
    always lands in the same bucket across processes and config reloads.
    """
    if mode == "legacy":
        if canary_percent is not None:
            raise EngineBindingError(
                "langgraph_canary_percent is only valid in mixed mode"
            )
        return 1
    if mode == "langgraph":
        if canary_percent is not None:
            raise EngineBindingError(
                "langgraph_canary_percent is only valid in mixed mode"
            )
        return 2
    # mixed
    if canary_percent is None:
        raise EngineBindingError("langgraph_canary_percent is required in mixed mode")
    if not 0 <= canary_percent <= 100:
        raise EngineBindingError("langgraph_canary_percent must be between 0 and 100")
    digest = sha256(agent_thread_id.bytes).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    if bucket < canary_percent:
        return 2
    return 1


def resolve_engine(
    *,
    workspace_flow_version: int,
    running_execution_engine: str | None = None,
    pending_engine: str | None = None,
) -> Engine:
    """Resolve the engine in the fixed fact order.

    ``running_execution_engine`` must be the engine of the Workspace's running
    execution (``status='running'``) and ``pending_engine`` the engine of its
    active/resuming pending row; abandoned tombstones never participate.  Any
    co-present disagreement raises the fixed
    :class:`EngineBindingConflictError` (409 ``ENGINE_BINDING_CONFLICT``)
    instead of forwarding the turn to another engine.
    """
    candidates: list[Engine] = []
    for raw in (running_execution_engine, pending_engine):
        if raw is None:
            continue
        if raw not in ("legacy", "langgraph"):
            raise EngineBindingError(f"unsupported engine: {raw}")
        candidates.append(cast(Engine, raw))
    candidates.append(flow_to_engine(workspace_flow_version))
    if len(set(candidates)) != 1:
        raise EngineBindingConflictError(
            "running execution, active pending and Workspace flow_version "
            "disagree on the bound engine"
        )
    return candidates[0]


class WorkspaceEngineResolver:
    """DB-backed engine resolution: one Workspace, one engine, both entries."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def resolve(self, *, workspace_token: str) -> Engine:
        """Resolve the Workspace's engine from the live application facts.

        JSON and SSE entries both call this same resolver, so a Workspace can
        never be split across engines by endpoint.
        """
        with self._session_factory() as session:
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
                )
            )
            if workspace is None:
                raise UnknownWorkspaceError(workspace_token)
            running_engine = session.scalar(
                select(AgentTurnExecutionRecord.engine)
                .where(
                    AgentTurnExecutionRecord.workspace_id == workspace.id,
                    AgentTurnExecutionRecord.status == "running",
                )
                .limit(1)
            )
            pending_engine = session.scalar(
                select(AgentPendingInputRecord.engine)
                .where(
                    AgentPendingInputRecord.workspace_id == workspace.id,
                    AgentPendingInputRecord.status.in_(_LIVE_PENDING_STATUSES),
                )
                .limit(1)
            )
            return resolve_engine(
                workspace_flow_version=workspace.flow_version,
                running_execution_engine=running_engine,
                pending_engine=pending_engine,
            )
