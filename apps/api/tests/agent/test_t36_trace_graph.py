"""T36 真实执行边界轨迹事件：节点、模型、RAG、工具与状态事实。

覆盖 Ticket T36 验收 2/3：node/model/retrieval/tool started/completed 只在
真实执行边界写入且顺序正确；LangGraph 模式不从 tool.summary 事后合成
started；只有 search_policies 产生 pgvector retrieval 事件；model
attempt/ordinal、工具 call identity 与真实节点路径符合 Spec §8.2；
确定性 event key 使同身份重放不产生第二组事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.identity import event_key as identity_event_key
from accesspilot.agent.identity import tool_call_id as identity_tool_call_id
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphRuntimeContext,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.structured_reply import MalformedStructuredOutputError
from accesspilot.agent.trace import (
    LANGGRAPH_GRAPH_VERSION,
    MODEL_STEP_KEY,
    GraphTraceRecorder,
)
from accesspilot.agent.turn_execution import StaleTurnFenceError
from accesspilot.auth import Principal
from accesspilot.db.models import (
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import ParsedReply, RequestDraft
from accesspilot.rag.policies import index_policy_embeddings
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService

REQUEST_TEXT = "申请客户数据导出 30 天，用于业务需要"


class StaticReplyModel:
    def __init__(self, reply: ParsedReply) -> None:
        self.reply = reply
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        return self.reply


class RetryReplyModel:
    def __init__(self, first: object, second: ParsedReply) -> None:
        self.first = first
        self.second = second
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        if self.calls == 1:
            if isinstance(self.first, BaseException):
                raise self.first
            return cast(ParsedReply, self.first)
        return self.second


@dataclass(frozen=True)
class T36GraphFixture:
    token: str
    workspace_id: UUID
    graph_run_id: UUID
    input_turn_id: str
    context: GraphRuntimeContext
    pending_input_id: UUID


def _graph_fixture(
    factory: sessionmaker[Session],
    *,
    model: object,
    draft: RequestDraft | None = None,
    pending: bool = False,
) -> T36GraphFixture:
    token = f"t36-graph-{uuid4()}"
    graph_run_id = uuid4()
    input_turn_id = f"turn-{uuid4()}"
    auth_session_id = uuid4()
    pending_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=1,
            draft=draft.model_dump(mode="json") if draft is not None else None,
            draft_revision=1 if draft is not None else 0,
            cursor_actor_id="EMP-001" if pending else None,
            cursor_auth_session_id=str(auth_session_id) if pending else None,
            cursor_expected_field="confirmation" if pending else None,
            cursor_last_question_kind="confirmation" if pending else None,
            cursor_issued_at=datetime.now(UTC) if pending else None,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        agent_thread_id = workspace.agent_thread_id
        session.add(
            AuthSessionRecord(
                id=auth_session_id,
                token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
                csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
                employee_id="EMP-001",
                workspace_id=workspace_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        input_event = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.user",
            payload={"content": "T36 fixture", "turn_id": input_turn_id},
        )
        session.add(input_event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=workspace_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                input_seq=0,
                input_turn_id=input_turn_id,
                input_event_id=input_event.id,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                status="running",
                checkpoint_ns="",
            )
        )
        if pending:
            from accesspilot.db.models import AgentPendingInputRecord

            session.add(
                AgentPendingInputRecord(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    agent_thread_id=agent_thread_id,
                    graph_run_id=graph_run_id,
                    checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                    pending_input_id=pending_id,
                    kind="confirmation",
                    draft_revision=1 if draft is not None else 0,
                    auth_session_ref=auth_session_id,
                    actor_id="EMP-001",
                    engine="langgraph",
                    checkpoint_ns="",
                    accepted_checkpoint_id="0" * 32,
                    status="active",
                )
            )
        session.commit()
    workspace_service = WorkspaceService(SqlAlchemyWorkspaceStore(factory))
    context: GraphRuntimeContext = {
        "session_factory": factory,
        "workspace_service": workspace_service,
        "policy_service": PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        ),
        "structured_reply_model": model,
        "intent_router": DeterministicIntentRouter(),
        "principal": Principal(
            employee_id="EMP-001",
            name="林晓",
            department="数据平台部",
            roles=("analyst",),
        ),
        "current_turn_id": input_turn_id,
        "current_fence": 1,
        "workspace_token": token,
        "auth_session_id": str(auth_session_id),
        "cookie": "runtime-cookie",
        "csrf_token": "runtime-csrf",
        "api_key": "runtime-key",
        "pending_input_id": str(pending_id),
        "trace_enabled": True,
    }
    return T36GraphFixture(
        token,
        workspace_id,
        graph_run_id,
        input_turn_id,
        context,
        pending_id,
    )


def _input(fixture: T36GraphFixture, content: str) -> GraphInput:
    return GraphInput(
        schema_version=1,
        flow_version=2,
        workspace_ref=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=0,
        input_turn_id=fixture.input_turn_id,
        safe_user_text=content,
        input_kind="new_input",
    )


def _events(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> list[WorkspaceEventRecord]:
    with factory() as session:
        return list(
            session.scalars(
                select(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == workspace_id)
                .order_by(WorkspaceEventRecord.id)
            ).all()
        )


@dataclass(frozen=True)
class T36RecorderFixture:
    token: str
    workspace_id: UUID
    graph_run_id: UUID
    input_turn_id: str
    auth_session_id: UUID
    actor_id: str
    input_seq: int
    execution_id: UUID


def _recorder_fixture(
    factory: sessionmaker[Session],
) -> T36RecorderFixture:
    """Create workspace + auth session + running fenced execution (fence=1)."""

    token = f"t36-recorder-{uuid4()}"
    graph_run_id = uuid4()
    input_turn_id = f"turn-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=1,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        session.add(
            AuthSessionRecord(
                id=auth_session_id,
                token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
                csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
                employee_id="EMP-001",
                workspace_id=workspace_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        input_event = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.user",
            payload={"content": "recorder fixture", "turn_id": input_turn_id},
        )
        session.add(input_event)
        session.flush()
        execution = AgentTurnExecutionRecord(
            workspace_id=workspace_id,
            graph_run_id=graph_run_id,
            checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
            input_seq=0,
            input_turn_id=input_turn_id,
            input_event_id=input_event.id,
            auth_session_ref=auth_session_id,
            actor_id="EMP-001",
            engine="langgraph",
            attempt=1,
            lease_fence=1,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            status="running",
            checkpoint_ns="",
        )
        session.add(execution)
        session.commit()
        execution_id = execution.id
    return T36RecorderFixture(
        token,
        workspace_id,
        graph_run_id,
        input_turn_id,
        auth_session_id,
        "EMP-001",
        0,
        execution_id,
    )


def _recorder(
    fixture: T36RecorderFixture,
    factory: sessionmaker[Session],
    *,
    lease_fence: int,
) -> GraphTraceRecorder:
    return GraphTraceRecorder(
        factory,
        workspace_token=fixture.token,
        input_turn_id=fixture.input_turn_id,
        actor_id=fixture.actor_id,
        auth_session_ref=str(fixture.auth_session_id),
        lease_fence=lease_fence,
    )


def _event_fingerprint(
    events: list[WorkspaceEventRecord],
) -> list[tuple[int, str, str | None]]:
    return [(event.id, event.event_type, event.event_key) for event in events]


def _complete_draft() -> RequestDraft:
    return RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="业务需要",
        confirmed=False,
    )


def _complete_reply() -> ParsedReply:
    return ParsedReply(
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="业务需要",
    )


def _config_for(graph_run_id: UUID) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": f"accesspilot:v1.3:{graph_run_id}",
            "checkpoint_ns": "",
        }
    }


# ---------------------------------------------------------------------------
# 只读工具路径：真实节点顺序与工具身份
# ---------------------------------------------------------------------------
def test_read_only_path_writes_real_node_and_tool_events_in_order(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
    )
    graph = build_production_graph(checkpointer=False)
    graph.invoke(_input(fixture, "我能申请什么"), context=fixture.context)

    events = _events(database_session_factory, fixture.workspace_id)
    types = [event.event_type for event in events]
    assert types == [
        "message.user",  # 执行行必须引用的输入事实（fixture 预写）
        "agent.node.started",
        "agent.node.completed",
        "agent.node.started",
        "agent.route.selected",
        "agent.node.completed",
        "agent.node.started",
        "agent.node.completed",
        "agent.node.started",
        "tool.started",
        "tool.completed",
        "agent.node.completed",
        "agent.node.started",
        "agent.node.completed",
        "agent.node.started",
        "agent.node.completed",
    ]

    nodes = [
        event.payload["node_code"]
        for event in events
        if event.event_type == "agent.node.started"
    ]
    assert nodes == [
        "hydrate_authoritative_snapshot",
        "route_intent",
        "select_read_tool",
        "execute_read_tool",
        "compose_safe_answer",
        "finalize_public_outcome",
    ]
    # 每个节点 started 先于自己的 completed，且顺序与真实路径一致。
    started_at: dict[str, int] = {}
    completed_at: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.event_type == "agent.node.started":
            started_at[str(event.payload["node_code"])] = index
        elif event.event_type == "agent.node.completed":
            completed_at[str(event.payload["node_code"])] = index
    for node in nodes:
        assert started_at[node] < completed_at[node], node

    route = next(
        event
        for event in events
        if event.event_type == "agent.route.selected"
    )
    assert route.payload["route_code"] == "read_only"
    assert route.payload["step_id"].startswith("stp_")

    tool_started = next(
        event for event in events if event.event_type == "tool.started"
    )
    tool_completed = next(
        event for event in events if event.event_type == "tool.completed"
    )
    assert tool_started.payload["tool"] == "list_eligible_access"
    assert tool_started.payload["tool_call_id"].startswith("tool_")
    assert tool_completed.payload["tool_call_id"] == tool_started.payload["tool_call_id"]
    assert tool_completed.payload["status"] == "success"
    # 工具 call identity 必须与 §8.2 的确定性编码一致。
    expected_call_id = identity_tool_call_id(
        workspace_id=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=0,
        tool_step_key="tool:list_eligible_access",
    )
    assert tool_started.payload["tool_call_id"] == expected_call_id
    # 工具事件不携带 raw 输出：summary 是有界安全摘要。
    assert 0 < len(str(tool_completed.payload["summary"])) <= 1_000


def test_langgraph_never_synthesizes_tool_started_from_tool_summary(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
    )
    graph = build_production_graph(checkpointer=False)
    graph.invoke(_input(fixture, "我能申请什么"), context=fixture.context)

    events = _events(database_session_factory, fixture.workspace_id)
    assert all(event.event_type != "tool.summary" for event in events)
    # started 只出现在真实执行边界：先于对应 completed，且携带稳定 step_id。
    tool_events = [
        event for event in events if event.event_type.startswith("tool.")
    ]
    assert [event.event_type for event in tool_events] == [
        "tool.started",
        "tool.completed",
    ]


# ---------------------------------------------------------------------------
# 政策 RAG：只有 search_policies 产生 retrieval 事件
# ---------------------------------------------------------------------------
def test_policy_path_emits_retrieval_events_and_never_tool_events(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
    )
    with database_session_factory() as session:
        assert index_policy_embeddings(session, DeterministicEmbeddingModel()) == 8
    graph = build_production_graph(checkpointer=False)
    graph.invoke(
        _input(fixture, "客户数据导出权限需要哪些审批？"),
        context=fixture.context,
    )

    events = _events(database_session_factory, fixture.workspace_id)
    started = [
        event for event in events if event.event_type == "retrieval.started"
    ]
    completed = [
        event for event in events if event.event_type == "retrieval.completed"
    ]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].payload["retriever"] == "pgvector"
    assert started[0].payload["step_id"].startswith("stp_")
    assert completed[0].payload["retriever"] == "pgvector"
    assert completed[0].payload["status"] == "grounded"
    assert completed[0].payload["match_count"] == len(
        completed[0].payload["evidence_codes"]
    )
    assert completed[0].payload["match_count"] > 0
    assert all(
        code.startswith("POL-")
        for code in completed[0].payload["evidence_codes"]
    )
    # search_policies 产生 retrieval 事件，绝不产生 tool 事件或 tool.summary。
    assert not [e for e in events if e.event_type.startswith("tool.")]
    assert not [e for e in events if e.event_type == "tool.summary"]


def test_retrieval_failure_still_writes_completed_with_safe_unavailable(
    database_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
    )
    from accesspilot.tools import policies as policy_module

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("provider down")

    monkeypatch.setattr(policy_module, "search_policies", explode)
    graph = build_production_graph(checkpointer=False)
    graph.invoke(
        _input(fixture, "客户数据导出权限需要哪些审批？"),
        context=fixture.context,
    )

    events = _events(database_session_factory, fixture.workspace_id)
    completed = [
        event for event in events if event.event_type == "retrieval.completed"
    ]
    assert len(completed) == 1
    assert completed[0].payload["status"] == "unavailable"
    assert completed[0].payload["match_count"] == 0
    assert completed[0].payload["evidence_codes"] == []
    # 原始异常细节绝不进入事件。
    assert "provider down" not in str(completed[0].payload)


# ---------------------------------------------------------------------------
# 模型边界：attempt 与零基 ordinal 身份
# ---------------------------------------------------------------------------
def test_request_path_emits_model_events_with_attempt_and_draft_updated(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(
            _input(fixture, REQUEST_TEXT),
            _config_for(fixture.graph_run_id),
            context=fixture.context,
        )

    events = _events(database_session_factory, fixture.workspace_id)
    started = [
        event for event in events if event.event_type == "model.started"
    ]
    completed = [
        event for event in events if event.event_type == "model.completed"
    ]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].payload["operation"] == "parse_input"
    assert started[0].payload["provider_mode"] == "mock"
    assert started[0].payload["attempt"] == 1
    assert completed[0].payload["status"] == "parsed"
    assert completed[0].payload["attempt"] == 1
    assert completed[0].payload["extracted_fields"] == [
        "entitlement_id",
        "duration_days",
        "justification",
    ]
    # attempt=1 → event identity ordinal=0（§8.2 固定映射）。
    expected_key = identity_event_key(
        workspace_id=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=0,
        step_key=MODEL_STEP_KEY,
        lifecycle_phase="completed",
        ordinal=0,
    )
    assert completed[0].event_key == expected_key
    assert started[0].event_key == identity_event_key(
        workspace_id=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=0,
        step_key=MODEL_STEP_KEY,
        lifecycle_phase="started",
        ordinal=0,
    )

    draft_events = [
        event for event in events if event.event_type == "draft.updated"
    ]
    assert len(draft_events) == 1
    assert draft_events[0].payload["draft_revision"] == 1
    assert draft_events[0].payload["missing_fields"] == []
    assert draft_events[0].payload["can_enter_approval"] is True
    assert draft_events[0].event_key is not None


def test_model_retry_uses_second_attempt_with_zero_based_ordinal_identity(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=RetryReplyModel(
            MalformedStructuredOutputError("第一轮格式错误"),
            _complete_reply(),
        ),
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(
            _input(fixture, REQUEST_TEXT),
            _config_for(fixture.graph_run_id),
            context=fixture.context,
        )

    model = fixture.context["structured_reply_model"]
    assert isinstance(model, RetryReplyModel)
    assert model.calls == 2
    events = _events(database_session_factory, fixture.workspace_id)
    started = [
        event for event in events if event.event_type == "model.started"
    ]
    completed = [
        event for event in events if event.event_type == "model.completed"
    ]
    assert [event.payload["attempt"] for event in started] == [1, 2]
    assert [event.payload["status"] for event in completed] == [
        "malformed",
        "parsed",
    ]
    keys = {event.event_key for event in started + completed}
    assert len(keys) == 4
    for attempt in (1, 2):
        ordinal = attempt - 1
        assert identity_event_key(
            workspace_id=fixture.workspace_id,
            graph_run_id=fixture.graph_run_id,
            input_seq=0,
            step_key=MODEL_STEP_KEY,
            lifecycle_phase="started",
            ordinal=ordinal,
        ) in keys
        assert identity_event_key(
            workspace_id=fixture.workspace_id,
            graph_run_id=fixture.graph_run_id,
            input_seq=0,
            step_key=MODEL_STEP_KEY,
            lifecycle_phase="completed",
            ordinal=ordinal,
        ) in keys


# ---------------------------------------------------------------------------
# 中断与恢复：agent.input 状态事实
# ---------------------------------------------------------------------------
def test_interrupt_then_resume_emits_input_resumed_with_decision(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
        draft=_complete_draft(),
        pending=True,
    )
    graph = build_production_graph(checkpointer=InMemorySaver())
    config = _config_for(fixture.graph_run_id)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(
            _input(fixture, REQUEST_TEXT),
            config,
            context=fixture.context,
        )

    graph.invoke(
        Command(resume={"decision": "confirm", "safe_user_text": "确认提交"}),
        config,
        context=fixture.context,
    )

    events = _events(database_session_factory, fixture.workspace_id)
    resumed = [
        event for event in events if event.event_type == "agent.input.resumed"
    ]
    assert len(resumed) == 1
    assert resumed[0].payload["pending_input_id"] == str(fixture.pending_input_id)
    assert resumed[0].payload["kind"] == "confirmation"
    assert resumed[0].payload["decision"] == "confirm"
    assert resumed[0].payload["step_id"].startswith("stp_")
    assert resumed[0].event_key is not None
    # 恢复是真实执行边界：等待节点在恢复轮重新执行并完成。
    awaiting_completed = [
        event
        for event in events
        if event.event_type == "agent.node.completed"
        and event.payload["node_code"] == "await_requester_confirmation"
    ]
    assert len(awaiting_completed) == 1


# ---------------------------------------------------------------------------
# 重放去重：确定性 event key 不产生第二组事件
# ---------------------------------------------------------------------------
def test_same_identity_replay_creates_no_second_event_set(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _graph_fixture(
        database_session_factory,
        model=StaticReplyModel(_complete_reply()),
    )
    graph = build_production_graph(checkpointer=False)
    graph_input = _input(fixture, "我能申请什么")
    graph.invoke(graph_input, context=fixture.context)
    first_run = _events(database_session_factory, fixture.workspace_id)
    trace_events = [
        event for event in first_run if event.event_type != "message.user"
    ]
    first_keys = {event.event_key for event in trace_events}
    assert all(key is not None for key in first_keys)
    assert len(first_keys) == len(trace_events)

    # 同一逻辑输入（同一 graph_run_id/input_seq）重放：节点再次执行，
    # 但事件身份相同，落库必须去重。
    graph.invoke(graph_input, context=fixture.context)
    second_run = _events(database_session_factory, fixture.workspace_id)

    assert len(second_run) == len(first_run)
    second_keys = {
        event.event_key
        for event in second_run
        if event.event_type != "message.user"
    }
    assert second_keys == first_keys
    for first, second in zip(first_run, second_run, strict=True):
        assert first.event_key == second.event_key
        assert first.event_type == second.event_type


def test_recorder_dedupes_duplicate_emission(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _recorder_fixture(database_session_factory)
    recorder = _recorder(fixture, database_session_factory, lease_fence=1)
    first = recorder.node_started(
        workspace_id=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=fixture.input_seq,
        turn_id=fixture.input_turn_id,
        node_code="route_intent",
    )
    second = recorder.node_started(
        workspace_id=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=fixture.input_seq,
        turn_id=fixture.input_turn_id,
        node_code="route_intent",
    )
    assert first is not None
    assert second is None
    assert [
        event.event_type
        for event in _events(database_session_factory, fixture.workspace_id)
    ] == [
        "message.user",
        "agent.node.started",
    ]
    assert first.event_key is not None
    assert first.event_key.startswith("evt_")


def test_stale_owner_after_takeover_cannot_write_trace_events(
    database_session_factory: sessionmaker[Session],
) -> None:
    """Spec §7/AC-05：takeover 递增 fence 后，旧 owner 的任何轨迹写入
    （新 key 与已存在 key）都必须以 STALE_TURN_FENCE 停止且零写入；
    新 owner 仍只产生一组确定性事件。"""

    fixture = _recorder_fixture(database_session_factory)
    old = _recorder(fixture, database_session_factory, lease_fence=1)
    first = old.node_started(
        workspace_id=fixture.workspace_id,
        graph_run_id=fixture.graph_run_id,
        input_seq=fixture.input_seq,
        turn_id=fixture.input_turn_id,
        node_code="route_intent",
    )
    assert first is not None

    # 接管：execution/workspace fence 1 -> 2，租约续期（takeover 语义）。
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.id == fixture.execution_id
            )
        )
        assert execution is not None
        execution.lease_fence = 2
        execution.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        workspace = session.get(WorkspaceRecord, fixture.workspace_id)
        assert workspace is not None
        workspace.lease_fence = 2
        session.commit()

    before = _event_fingerprint(
        _events(database_session_factory, fixture.workspace_id)
    )
    # 旧 owner 写新 key：拒绝且零写入。
    with pytest.raises(StaleTurnFenceError):
        old.node_completed(
            workspace_id=fixture.workspace_id,
            graph_run_id=fixture.graph_run_id,
            input_seq=fixture.input_seq,
            turn_id=fixture.input_turn_id,
            node_code="route_intent",
            status="success",
        )
    # 旧 owner 写已存在 key：同样拒绝，不能因 event_key 已存在而静默成功。
    with pytest.raises(StaleTurnFenceError):
        old.node_started(
            workspace_id=fixture.workspace_id,
            graph_run_id=fixture.graph_run_id,
            input_seq=fixture.input_seq,
            turn_id=fixture.input_turn_id,
            node_code="route_intent",
        )
    assert _event_fingerprint(
        _events(database_session_factory, fixture.workspace_id)
    ) == before

    # 新 owner（fence=2）继续补齐缺失事件，已存在 key 去重：仍只有一组。
    new = _recorder(fixture, database_session_factory, lease_fence=2)
    assert (
        new.node_started(
            workspace_id=fixture.workspace_id,
            graph_run_id=fixture.graph_run_id,
            input_seq=fixture.input_seq,
            turn_id=fixture.input_turn_id,
            node_code="route_intent",
        )
        is None
    )
    assert (
        new.node_completed(
            workspace_id=fixture.workspace_id,
            graph_run_id=fixture.graph_run_id,
            input_seq=fixture.input_seq,
            turn_id=fixture.input_turn_id,
            node_code="route_intent",
            status="success",
        )
        is not None
    )
    after = _events(database_session_factory, fixture.workspace_id)
    assert [event.event_type for event in after] == [
        "message.user",
        "agent.node.started",
        "agent.node.completed",
    ]
    keys = {event.event_key for event in after if event.event_key is not None}
    assert len(keys) == len(after) - 1


def test_recorder_rejects_token_workspace_mismatch_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    """token 指向的 Workspace 与传入 workspace_id 不一致时零写入。"""

    fixture_a = _recorder_fixture(database_session_factory)
    fixture_b = _recorder_fixture(database_session_factory)
    recorder = _recorder(fixture_a, database_session_factory, lease_fence=1)
    with pytest.raises(StaleTurnFenceError):
        recorder.node_started(
            workspace_id=fixture_b.workspace_id,
            graph_run_id=fixture_a.graph_run_id,
            input_seq=fixture_a.input_seq,
            turn_id=fixture_a.input_turn_id,
            node_code="route_intent",
        )
    assert [
        event.event_type
        for event in _events(database_session_factory, fixture_a.workspace_id)
    ] == ["message.user"]
    assert [
        event.event_type
        for event in _events(database_session_factory, fixture_b.workspace_id)
    ] == ["message.user"]


def test_turn_started_graph_version_constant_is_stable() -> None:
    assert LANGGRAPH_GRAPH_VERSION == "accesspilot-langgraph-v1.3"
