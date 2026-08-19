"""T36 真实执行边界轨迹事件记录器（脱敏、可去重、重放安全）。

每条事件在真实图节点、模型调用、政策 RAG、只读工具或状态写入边界产生，
落库前经过 events.validate_event_payload 的 Schema/禁用 key/敏感值扫描，
并使用 identity 模块的确定性 step_id/event_key/tool_call_id 去重：
checkpoint 重放同一逻辑输入不会产生第二组事件。execution attempt、lease
fence、HTTP 重试与随机 UUID 一律不进入身份（Spec §8.2）。

轨迹记录器是图运行时的能力注入，不改变图语义；入口适配器（T38/T40）在
生产调用时显式启用 ``trace_enabled``。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.identity import (
    event_key as identity_event_key,
)
from accesspilot.agent.identity import (
    model_attempt_to_event_ordinal,
)
from accesspilot.agent.identity import (
    step_id as identity_step_id,
)
from accesspilot.agent.identity import (
    tool_call_id as identity_tool_call_id,
)
from accesspilot.db.models import WorkspaceEventRecord
from accesspilot.events import stage_workspace_event

LANGGRAPH_GRAPH_VERSION = "accesspilot-langgraph-v1.3"

# 稳定步骤身份：与 T28 golden vectors 保持同一 canonical 约定。
ROUTE_STEP_KEY = "route_intent"
MODEL_STEP_KEY = "model:parse_input"
RETRIEVAL_STEP_KEY = "retrieval:pgvector"
INPUT_STEP_KEY = "await_requester_confirmation"
DRAFT_CAS_STEP_KEY = "persist_draft_cas"
NUMERIC_DRAFT_STEP_KEY = "persist_numeric_duration"

# 每个真实节点对用户可见的稳定文案（虚构产品文案）。
NODE_PUBLIC_LABELS: dict[str, str] = {
    "hydrate_authoritative_snapshot": "载入会话与草稿",
    "route_intent": "识别意图与路由",
    "compose_safe_answer": "生成安全答复",
    "handle_numeric_followup": "处理数字补全",
    "select_read_tool": "选择只读工具",
    "execute_read_tool": "执行只读工具",
    "retrieve_policy_pgvector": "检索政策知识库",
    "grade_policy_evidence": "评估政策证据",
    "compose_grounded_answer": "生成有依据答复",
    "compose_insufficient_answer": "生成证据不足答复",
    "compose_recoverable_answer": "生成可恢复错误答复",
    "parse_request_patch": "解析申请字段",
    "resolve_entitlement": "解析权限编码",
    "merge_candidate": "合并申请草稿",
    "persist_draft_cas": "保存申请草稿",
    "validate_draft": "校验申请草稿",
    "ask_missing_field": "追问缺失字段",
    "await_requester_confirmation": "等待申请人确认",
    "rehydrate_resume_snapshot": "恢复会话快照",
    "apply_confirmation_cas": "应用确认",
    "ready_to_submit": "准备提交",
    "finalize_public_outcome": "生成最终答复",
}


def node_public_label(node_code: str) -> str:
    """未知节点安全降级为稳定占位文案，绝不返回内部细节。"""

    return NODE_PUBLIC_LABELS.get(node_code, "执行内部步骤")


def terminal_step_key(event_type: str) -> str:
    return f"finalize:{event_type}"


def tool_step_key(tool_name: str) -> str:
    return f"tool:{tool_name}"


class GraphTraceRecorder:
    """在真实执行边界把脱敏轨迹事件写入 Workspace 事件表。

    记录器绑定服务端验证的 execution context（input_turn_id/actor_id/
    auth_session_ref/lease_fence）；每个事件事务按既有锁顺序（execution ->
    workspace）先锁定并复核 running execution、未过期 lease、AuthSession
    与 Workspace 当前 fence，再执行 event-key 去重和落库。任何
    stale/mismatch 都以稳定 STALE_TURN_FENCE 语义停止且零写入，即使目标
    event_key 已存在也不会静默成功。同一 (workspace_id, event_key) 只对
    当前 owner 落库一次，partial unique index 兜底并发重放。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        workspace_token: str,
        input_turn_id: str,
        actor_id: str,
        auth_session_ref: str,
        lease_fence: int,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("trace recorder requires a session factory")
        if not isinstance(workspace_token, str) or not workspace_token.strip():
            raise ValueError("workspace_token must be a non-empty string")
        if not isinstance(input_turn_id, str) or not input_turn_id:
            raise ValueError("input_turn_id must be a non-empty string")
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("actor_id must be a non-empty string")
        if type(lease_fence) is not int or lease_fence < 1:
            raise ValueError("lease_fence must be a positive integer")
        try:
            parsed_session_ref = UUID(auth_session_ref)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("auth_session_ref must be a UUID") from None
        self._session_factory = session_factory
        self._workspace_token = workspace_token
        self._input_turn_id = input_turn_id
        self._actor_id = actor_id
        self._auth_session_ref = parsed_session_ref
        self._lease_fence = lease_fence

    # ------------------------------------------------------------------
    # 通用落库
    # ------------------------------------------------------------------
    def _verify_fenced_execution(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
    ) -> None:
        """Spec §7 fenced-write 门禁：execution -> workspace 锁序复核。

        任一坐标不一致（token/Workspace、turn、actor、session、fence、
        status、lease）都整体回滚并抛出稳定 STALE_TURN_FENCE。
        """

        from accesspilot.agent.turn_execution import StaleTurnFenceError
        from accesspilot.db.models import (
            AgentTurnExecutionRecord,
            AuthSessionRecord,
            WorkspaceRecord,
        )
        from accesspilot.db.workspace_store import hash_workspace_token

        now = datetime.now(UTC)
        execution = session.scalar(
            select(AgentTurnExecutionRecord)
            .where(
                AgentTurnExecutionRecord.workspace_id == workspace_id,
                AgentTurnExecutionRecord.graph_run_id == graph_run_id,
                AgentTurnExecutionRecord.input_seq == input_seq,
                AgentTurnExecutionRecord.input_turn_id == self._input_turn_id,
                AgentTurnExecutionRecord.actor_id == self._actor_id,
                AgentTurnExecutionRecord.auth_session_ref
                == self._auth_session_ref,
                AgentTurnExecutionRecord.lease_fence == self._lease_fence,
                AgentTurnExecutionRecord.engine == "langgraph",
                AgentTurnExecutionRecord.status == "running",
            )
            .with_for_update()
        )
        if (
            execution is None
            or execution.lease_expires_at is None
            or execution.lease_expires_at <= now
        ):
            raise StaleTurnFenceError(
                "trace execution fence is stale or mismatched"
            )
        auth_session = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.id == self._auth_session_ref,
                AuthSessionRecord.workspace_id == workspace_id,
                AuthSessionRecord.employee_id == self._actor_id,
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > now,
            )
        )
        if auth_session is None:
            raise StaleTurnFenceError(
                "trace auth session is invalid or expired"
            )
        workspace = session.scalar(
            select(WorkspaceRecord)
            .where(
                WorkspaceRecord.id == workspace_id,
                WorkspaceRecord.token_hash
                == hash_workspace_token(self._workspace_token),
                WorkspaceRecord.actor_id == self._actor_id,
                WorkspaceRecord.lease_fence == self._lease_fence,
            )
            .with_for_update()
        )
        if workspace is None:
            raise StaleTurnFenceError(
                "trace workspace fence is stale or mismatched"
            )

    def _emit(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        event_type: str,
        step_key: str,
        lifecycle_phase: str,
        ordinal: int,
        payload: dict[str, object],
    ) -> WorkspaceEventRecord | None:
        from accesspilot.agent.turn_execution import StaleTurnFenceError

        if turn_id != self._input_turn_id:
            raise StaleTurnFenceError(
                "trace event turn does not match the fenced execution"
            )
        key = identity_event_key(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            step_key=step_key,
            lifecycle_phase=lifecycle_phase,
            ordinal=ordinal,
        )
        with self._session_factory() as session:
            self._verify_fenced_execution(
                session,
                workspace_id=workspace_id,
                graph_run_id=graph_run_id,
                input_seq=input_seq,
            )
            existing = session.scalar(
                select(WorkspaceEventRecord.id)
                .where(
                    WorkspaceEventRecord.workspace_id == workspace_id,
                    WorkspaceEventRecord.event_key == key,
                )
                .limit(1)
            )
            if existing is not None:
                return None
            record = stage_workspace_event(
                session,
                workspace_token=self._workspace_token,
                event_type=event_type,
                payload=payload,
                event_key=key,
            )
            try:
                session.commit()
            except IntegrityError:
                # 当前 owner 并发重放同时写入同一事件：partial unique index
                # 兜底，按已存在处理；stale/mismatch 已在门禁处抛出，不会
                # 走到这里静默成功。
                session.rollback()
                return None
            session.refresh(record)
            return record

    def _step_id(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        step_key: str,
    ) -> str:
        return identity_step_id(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            step_key=step_key,
        )

    # ------------------------------------------------------------------
    # 节点边界
    # ------------------------------------------------------------------
    def node_started(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        node_code: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="agent.node.started",
            step_key=node_code,
            lifecycle_phase="started",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=node_code,
                ),
                "node_code": node_code,
                "public_label": node_public_label(node_code),
                "status": "running",
            },
        )

    def node_completed(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        node_code: str,
        status: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="agent.node.completed",
            step_key=node_code,
            lifecycle_phase="completed",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=node_code,
                ),
                "node_code": node_code,
                "public_label": node_public_label(node_code),
                "status": status,
            },
        )

    # ------------------------------------------------------------------
    # 路由边界
    # ------------------------------------------------------------------
    def route_selected(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        route_code: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="agent.route.selected",
            step_key=ROUTE_STEP_KEY,
            lifecycle_phase="selected",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=ROUTE_STEP_KEY,
                ),
                "route_code": route_code,
            },
        )

    # ------------------------------------------------------------------
    # 模型边界
    # ------------------------------------------------------------------
    def model_started(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        operation: str,
        provider_mode: str,
        attempt: int,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="model.started",
            step_key=MODEL_STEP_KEY,
            lifecycle_phase="started",
            ordinal=model_attempt_to_event_ordinal(attempt),
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=MODEL_STEP_KEY,
                ),
                "operation": operation,
                "provider_mode": provider_mode,
                "attempt": attempt,
            },
        )

    def model_completed(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        operation: str,
        provider_mode: str,
        attempt: int,
        status: str,
        extracted_fields: list[str],
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="model.completed",
            step_key=MODEL_STEP_KEY,
            lifecycle_phase="completed",
            ordinal=model_attempt_to_event_ordinal(attempt),
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=MODEL_STEP_KEY,
                ),
                "operation": operation,
                "provider_mode": provider_mode,
                "attempt": attempt,
                "status": status,
                "extracted_fields": extracted_fields,
            },
        )

    # ------------------------------------------------------------------
    # pgvector 检索边界
    # ------------------------------------------------------------------
    def retrieval_started(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="retrieval.started",
            step_key=RETRIEVAL_STEP_KEY,
            lifecycle_phase="started",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=RETRIEVAL_STEP_KEY,
                ),
                "retriever": "pgvector",
            },
        )

    def retrieval_completed(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        status: str,
        match_count: int,
        evidence_codes: list[str],
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="retrieval.completed",
            step_key=RETRIEVAL_STEP_KEY,
            lifecycle_phase="completed",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=RETRIEVAL_STEP_KEY,
                ),
                "retriever": "pgvector",
                "status": status,
                "match_count": match_count,
                "evidence_codes": evidence_codes,
            },
        )

    # ------------------------------------------------------------------
    # 只读工具边界
    # ------------------------------------------------------------------
    def tool_started(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        tool: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="tool.started",
            step_key=tool_step_key(tool),
            lifecycle_phase="started",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "tool": tool,
                "tool_call_id": identity_tool_call_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    tool_step_key=tool_step_key(tool),
                ),
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=tool_step_key(tool),
                ),
            },
        )

    def tool_completed(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        tool: str,
        status: str,
        summary: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="tool.completed",
            step_key=tool_step_key(tool),
            lifecycle_phase="completed",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "tool": tool,
                "tool_call_id": identity_tool_call_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    tool_step_key=tool_step_key(tool),
                ),
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=tool_step_key(tool),
                ),
                "status": status,
                "summary": summary,
            },
        )

    # ------------------------------------------------------------------
    # HITL 状态边界
    # ------------------------------------------------------------------
    def input_resumed(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        pending_input_id: str,
        decision: str,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="agent.input.resumed",
            step_key=INPUT_STEP_KEY,
            lifecycle_phase="resumed",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "step_id": self._step_id(
                    workspace_id=workspace_id,
                    graph_run_id=graph_run_id,
                    input_seq=input_seq,
                    step_key=INPUT_STEP_KEY,
                ),
                "pending_input_id": pending_input_id,
                "kind": "confirmation",
                "decision": decision,
            },
        )

    # ------------------------------------------------------------------
    # 草稿状态边界
    # ------------------------------------------------------------------
    def draft_updated(
        self,
        *,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        turn_id: str,
        step_key: str,
        draft: dict[str, Any],
        missing_fields: Sequence[str],
        can_enter_approval: bool,
        draft_revision: int,
    ) -> WorkspaceEventRecord | None:
        return self._emit(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            input_seq=input_seq,
            turn_id=turn_id,
            event_type="draft.updated",
            step_key=step_key,
            lifecycle_phase="updated",
            ordinal=0,
            payload={
                "turn_id": turn_id,
                "draft": draft,
                "missing_fields": missing_fields,
                "can_enter_approval": can_enter_approval,
                "draft_revision": draft_revision,
            },
        )
