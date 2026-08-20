"""T40 SSE LangGraph gate, worker lifecycle, and sticky canary contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.session import close_all_sessions

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.json_orchestrator import LangGraphConversationOrchestrator
from accesspilot.agent.turn_execution import TurnExecutionService
from accesspilot.config import Settings
from accesspilot.conversation import (
    DeterministicStructuredReplyModel,
    normalized_outcome,
)
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.main import create_app
from accesspilot.streaming import DeterministicAnswerStreamModel
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService
from db.test_t34_postgres import _isolated_checkpoint_database
from support.auth import login_as


class _InMemoryCheckpointRuntime:
    def __init__(self) -> None:
        self.saver = InMemorySaver()
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def check_readiness(self) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


class _BlockingStructuredReplyModel:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def parse_reply(self, user_reply: str, correction: str | None = None):
        del user_reply, correction
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test worker release timed out")
        return DeterministicStructuredReplyModel().parse_reply(
            "申请 insighthub.dashboard_view 14天，为了 T40 断线验收"
        )


class _BrokenAnswerStream:
    async def stream_answer(
        self,
        *,
        assistant_message: str,
        turn_id: str,
    ) -> AsyncIterator[str]:
        del assistant_message, turn_id
        raise RuntimeError("unsafe provider detail")
        yield "unreachable"


def _parse_sse_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()
        if "event" in fields and "data" in fields:
            frames.append(
                {
                    "id": fields.get("id"),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return frames


def _legacy_settings() -> Settings:
    return Settings(
        orchestrator_mode="legacy",
        langgraph_canary_percent=None,
        _env_file=None,
    )


def _mixed_settings(percent: int) -> Settings:
    return Settings(
        orchestrator_mode="mixed",
        langgraph_canary_percent=percent,
        checkpoint_database_url="postgresql://runtime/db",
        demo_mode_enabled=True,
        _env_file=None,
    )


def _langgraph_orchestrator(
    factory: sessionmaker[Session],
    saver: InMemorySaver,
) -> LangGraphConversationOrchestrator:
    return LangGraphConversationOrchestrator(
        session_factory=factory,
        workspace_service=WorkspaceService(SqlAlchemyWorkspaceStore(factory)),
        model=DeterministicStructuredReplyModel(),
        policy_service=PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        ),
        checkpoint_saver=saver,
    )


async def _direct_asgi_sse_request(
    app: Any,
    *,
    session_token: str,
    csrf_token: str,
    content: str,
) -> tuple[int, bytes]:
    body = json.dumps({"content": content}, ensure_ascii=False).encode()
    request_sent = False
    sent_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat/messages/stream",
            "raw_path": b"/api/chat/messages/stream",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cookie", f"accesspilot_session={session_token}".encode()),
                (b"origin", b"http://127.0.0.1:5173"),
                (b"x-csrf-token", csrf_token.encode()),
            ],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        int(message["status"])
        for message in sent_messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        bytes(message.get("body", b""))
        for message in sent_messages
        if message["type"] == "http.response.body"
    )
    return status, response_body


def _admission_fact_counts(
    factory: sessionmaker[Session],
    *,
    workspace_id: UUID,
) -> tuple[int, int, int]:
    with factory() as session:
        event_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(WorkspaceEventRecord.workspace_id == workspace_id)
        )
        input_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace_id,
                WorkspaceEventRecord.event_type == "message.user",
            )
        )
        execution_count = session.scalar(
            select(func.count())
            .select_from(AgentTurnExecutionRecord)
            .where(AgentTurnExecutionRecord.workspace_id == workspace_id)
        )
        assert event_count is not None
        assert input_count is not None
        assert execution_count is not None
        return event_count, input_count, execution_count


def test_injected_sse_enters_real_graph_with_one_http_turn_identity(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC1: the isolated SSE gate executes nodes, not a Legacy wrapper."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=_langgraph_orchestrator(
            database_session_factory,
            InMemorySaver(),
        ),
        answer_stream_model=DeterministicAnswerStreamModel(),
    )

    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
        response = client.post(
            "/api/chat/messages/stream",
            json={"content": "帮助"},
        )

    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    terminal_names = {
        "message.completed",
        "error.recoverable",
        "turn.interrupted",
    }
    assert [frame["event"] for frame in frames if frame["event"] in terminal_names] == [
        "message.completed"
    ]
    assert all(
        frame["event"] not in {"debug", "values", "checkpoint", "checkpoint.raw"}
        for frame in frames
    )
    turn_id = frames[0]["data"]["turn_id"]
    assert all(frame["data"]["turn_id"] == turn_id for frame in frames)
    assert [frame["data"]["seq"] for frame in frames] == list(
        range(1, len(frames) + 1)
    )
    assert all(
        frame["id"] == f"{turn_id}:{frame['data']['seq']}" for frame in frames
    )

    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(
                    login.session_token
                )
            )
        )
        assert workspace is not None
        events = session.scalars(
            select(WorkspaceEventRecord)
            .where(WorkspaceEventRecord.workspace_id == workspace.id)
            .order_by(WorkspaceEventRecord.id)
        ).all()
    starts = [event for event in events if event.event_type == "turn.started"]
    assert len(starts) == 1
    assert starts[0].payload["turn_id"] == turn_id
    assert starts[0].payload["orchestrator"] == "langgraph"
    node_codes = [
        event.payload["node_code"]
        for event in events
        if event.event_type == "agent.node.completed"
    ]
    assert node_codes == [
        "hydrate_authoritative_snapshot",
        "route_intent",
        "compose_safe_answer",
        "finalize_public_outcome",
    ]
    assert all(
        event.payload.get("turn_id") == turn_id
        for event in events
        if event.event_type.startswith("agent.node.")
    )


def test_mixed_nonzero_canary_is_valid_after_both_entry_gates(monkeypatch) -> None:
    """AC3: T40 opens nonzero mixed allocation while retaining strict guards."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    settings = Settings(
        orchestrator_mode="mixed",
        langgraph_canary_percent=100,
        checkpoint_database_url="postgresql://runtime/db",
        _env_file=None,
    )

    assert settings.orchestrator_mode == "mixed"
    assert settings.langgraph_canary_percent == 100


def test_product_mixed_flow_two_routes_json_and_sse_to_one_graph_engine(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC3: lifespan saver wiring and sticky dispatch are product behavior."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    runtime = _InMemoryCheckpointRuntime()
    app = create_app(
        settings=_mixed_settings(100),
        session_factory=database_session_factory,
        checkpoint_runtime_factory=lambda settings: runtime,
        answer_stream_model=DeterministicAnswerStreamModel(),
    )

    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
        json_response = client.post(
            "/api/chat/messages",
            json={"content": "帮助"},
        )
        sse_response = client.post(
            "/api/chat/messages/stream",
            json={"content": "帮助"},
        )

    assert json_response.status_code == 200
    assert sse_response.status_code == 200
    assert json_response.json()["business_status"] == "answered"
    completed = next(
        frame
        for frame in _parse_sse_frames(sse_response.text)
        if frame["event"] == "message.completed"
    )
    assert completed["data"]["payload"]["business_status"] == "answered"
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash
                == hash_workspace_token(login.session_token)
            )
        )
        assert workspace is not None and workspace.flow_version == 2
        executions = session.scalars(
            select(AgentTurnExecutionRecord)
            .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
            .order_by(AgentTurnExecutionRecord.input_seq)
        ).all()
        starts = session.scalars(
            select(WorkspaceEventRecord).where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.event_type == "turn.started",
            )
        ).all()
    assert [execution.engine for execution in executions] == [
        "langgraph",
        "langgraph",
    ]
    assert [execution.status for execution in executions] == [
        "completed",
        "completed",
    ]
    assert all(event.payload["orchestrator"] == "langgraph" for event in starts)
    assert all(event.payload["flow_version"] == 2 for event in starts)
    assert runtime.started == 1
    assert runtime.closed == 1


def test_product_flow_two_resumes_json_to_sse_and_sse_to_json(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC3: pending/resume crosses entries without changing run or engine."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    runtime = _InMemoryCheckpointRuntime()
    app = create_app(
        settings=_mixed_settings(100),
        session_factory=database_session_factory,
        checkpoint_runtime_factory=lambda settings: runtime,
        answer_stream_model=DeterministicAnswerStreamModel(),
    )

    with TestClient(app) as client:
        first_login = login_as(client, session_factory=database_session_factory)
        json_pending = client.post(
            "/api/chat/messages",
            json={
                "content": (
                    "申请 insighthub.dashboard_view 14天，"
                    "为了 T40 JSON 到 SSE 验收"
                )
            },
        )
        sse_confirm = client.post(
            "/api/chat/messages/stream",
            json={"content": "确认提交"},
        )

        second_login = login_as(client, session_factory=database_session_factory)
        sse_pending = client.post(
            "/api/chat/messages/stream",
            json={
                "content": (
                    "申请 insighthub.dashboard_view 14天，"
                    "为了 T40 SSE 到 JSON 验收"
                )
            },
        )
        json_confirm = client.post(
            "/api/chat/messages",
            json={"content": "确认提交"},
        )

    assert json_pending.json()["business_status"] == "awaiting_confirmation"
    assert next(
        frame
        for frame in _parse_sse_frames(sse_confirm.text)
        if frame["event"] == "message.completed"
    )["data"]["payload"]["business_status"] == "ready_to_submit"
    assert next(
        frame
        for frame in _parse_sse_frames(sse_pending.text)
        if frame["event"] == "message.completed"
    )["data"]["payload"]["business_status"] == "awaiting_confirmation"
    assert json_confirm.json()["business_status"] == "ready_to_submit"

    for login in (first_login, second_login):
        with database_session_factory() as session:
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash
                    == hash_workspace_token(login.session_token)
                )
            )
            assert workspace is not None and workspace.flow_version == 2
            executions = session.scalars(
                select(AgentTurnExecutionRecord)
                .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
                .order_by(AgentTurnExecutionRecord.input_seq)
            ).all()
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.workspace_id == workspace.id
                )
            )
            terminal_count = session.scalar(
                select(func.count())
                .select_from(WorkspaceEventRecord)
                .where(
                    WorkspaceEventRecord.workspace_id == workspace.id,
                    WorkspaceEventRecord.event_type.in_(
                        (
                            "message.completed",
                            "error.recoverable",
                            "turn.interrupted",
                        )
                    ),
                )
            )
        assert len(executions) == 2
        assert executions[0].graph_run_id == executions[1].graph_run_id
        assert [execution.engine for execution in executions] == [
            "langgraph",
            "langgraph",
        ]
        assert pending is not None and pending.status == "resolved"
        assert terminal_count == 2


def test_product_mixed_flow_one_keeps_both_entries_on_legacy(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC3: a canary configuration never moves an existing flow-1 Workspace."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    runtime = _InMemoryCheckpointRuntime()
    app = create_app(
        settings=_mixed_settings(0),
        session_factory=database_session_factory,
        checkpoint_runtime_factory=lambda settings: runtime,
    )

    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
        json_response = client.post(
            "/api/chat/messages",
            json={"content": "帮助"},
        )
        sse_response = client.post(
            "/api/chat/messages/stream",
            json={"content": "帮助"},
        )

    assert json_response.status_code == 200
    assert sse_response.status_code == 200
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash
                == hash_workspace_token(login.session_token)
            )
        )
        assert workspace is not None and workspace.flow_version == 1
        execution_count = session.scalar(
            select(func.count())
            .select_from(AgentTurnExecutionRecord)
            .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
        )
        starts = session.scalars(
            select(WorkspaceEventRecord).where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.event_type == "turn.started",
            )
        ).all()
    assert execution_count == 0
    assert all(event.payload["orchestrator"] == "legacy" for event in starts)
    assert all(event.payload["flow_version"] == 1 for event in starts)


def test_engine_binding_conflict_is_409_before_both_entries_execute(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC3: pending engine and Workspace flow disagreement never forwards."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    runtime = _InMemoryCheckpointRuntime()
    app = create_app(
        settings=_mixed_settings(100),
        session_factory=database_session_factory,
        checkpoint_runtime_factory=lambda settings: runtime,
    )

    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
        pending_response = client.post(
            "/api/chat/messages",
            json={
                "content": (
                    "申请 insighthub.dashboard_view 14天，"
                    "为了 T40 绑定冲突验收"
                )
            },
        )
        assert pending_response.status_code == 200
        with database_session_factory() as session:
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash
                    == hash_workspace_token(login.session_token)
                )
            )
            assert workspace is not None
            workspace.flow_version = 1
            session.commit()
        json_conflict = client.post(
            "/api/chat/messages",
            json={"content": "确认提交"},
        )
        sse_conflict = client.post(
            "/api/chat/messages/stream",
            json={"content": "确认提交"},
        )

    expected = {
        "detail": {
            "code": "ENGINE_BINDING_CONFLICT",
            "message": "Workspace 引擎绑定状态不一致。",
        }
    }
    assert json_conflict.status_code == 409
    assert json_conflict.json() == expected
    assert sse_conflict.status_code == 409
    assert sse_conflict.json() == expected
    with database_session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AgentTurnExecutionRecord)
            .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
        ) == 1


def test_graph_answer_error_owns_error_terminal_without_pseudo_success(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC1: answer failure promotes one head with error, never prior success."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=_langgraph_orchestrator(
            database_session_factory,
            InMemorySaver(),
        ),
        answer_stream_model=_BrokenAnswerStream(),
    )

    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
        response = client.post(
            "/api/chat/messages/stream",
            json={"content": "帮助"},
        )

    frames = _parse_sse_frames(response.text)
    assert [
        frame["event"]
        for frame in frames
        if frame["event"]
        in {"message.completed", "error.recoverable", "turn.interrupted"}
    ] == ["error.recoverable"]
    assert "unsafe provider detail" not in response.text
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash
                == hash_workspace_token(login.session_token)
            )
        )
        assert workspace is not None
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace.id
            )
        )
        assert execution is not None
        terminal_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.event_type.in_(
                    ("message.completed", "error.recoverable", "turn.interrupted")
                ),
            )
        )
    assert execution.status == "recoverable_error"
    assert execution.accepted_checkpoint_id is not None
    assert execution.lease_expires_at is None
    assert terminal_count == 1


def test_graph_interrupt_answer_error_keeps_pending_head_but_not_success(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC1: an interrupt projection stays checkpoint-consistent on stream error."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=_langgraph_orchestrator(
            database_session_factory,
            InMemorySaver(),
        ),
        answer_stream_model=_BrokenAnswerStream(),
    )

    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
        response = client.post(
            "/api/chat/messages/stream",
            json={
                "content": (
                    "申请 insighthub.dashboard_view 14天，"
                    "为了 T40 interrupt answer 错误验收"
                )
            },
        )

    frames = _parse_sse_frames(response.text)
    assert [
        frame["event"]
        for frame in frames
        if frame["event"]
        in {"message.completed", "error.recoverable", "turn.interrupted"}
    ] == ["error.recoverable"]
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash
                == hash_workspace_token(login.session_token)
            )
        )
        assert workspace is not None
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace.id
            )
        )
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.workspace_id == workspace.id
            )
        )
    assert execution is not None and execution.status == "recoverable_error"
    assert execution.accepted_checkpoint_id is not None
    assert pending is not None and pending.status == "active"
    assert pending.accepted_checkpoint_id == execution.accepted_checkpoint_id
    assert (
        WorkspaceService(SqlAlchemyWorkspaceStore(database_session_factory))
        .get(login.session_token)
        .active_cursor()
        is not None
    )


async def _disconnect_while_graph_worker_is_blocked(
    app: Any,
    *,
    session_token: str,
    csrf_token: str,
    model: _BlockingStructuredReplyModel,
    factory: sessionmaker[Session],
) -> tuple[str, bool, int, str, bool, int]:
    body = json.dumps(
        {
            "content": (
                "申请 insighthub.dashboard_view 14天，"
                "为了 T40 worker 断线验收"
            )
        },
        ensure_ascii=False,
    ).encode()
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        worker_started = await asyncio.to_thread(model.started.wait, 5)
        assert worker_started
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        del message

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat/messages/stream",
            "raw_path": b"/api/chat/messages/stream",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cookie", f"accesspilot_session={session_token}".encode()),
                (b"origin", b"http://127.0.0.1:5173"),
                (b"x-csrf-token", csrf_token.encode()),
            ],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    with factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(session_token)
            )
        )
        assert workspace is not None
        before = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace.id
            )
        )
        before_terminal_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.event_type.in_(
                    ("message.completed", "error.recoverable", "turn.interrupted")
                ),
            )
        )
        assert before is not None and before_terminal_count is not None
        before_facts = (
            before.status,
            before.lease_expires_at is not None,
            before_terminal_count,
        )

    model.release.set()
    for _ in range(100):
        if not app.state.stream_cleanup_tasks:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("disconnect cleanup did not finish")
    with factory() as session:
        after = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace.id
            )
        )
        after_terminal_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.event_type.in_(
                    ("message.completed", "error.recoverable", "turn.interrupted")
                ),
            )
        )
        assert after is not None and after_terminal_count is not None
        return (
            *before_facts,
            after.status,
            after.lease_expires_at is not None,
            after_terminal_count,
        )


def test_direct_asgi_disconnect_waits_for_graph_worker_before_interrupted(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC2: disconnect cannot cancel/release an active synchronous worker."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    model = _BlockingStructuredReplyModel()
    orchestrator = LangGraphConversationOrchestrator(
        session_factory=database_session_factory,
        workspace_service=WorkspaceService(
            SqlAlchemyWorkspaceStore(database_session_factory)
        ),
        model=model,
        checkpoint_saver=InMemorySaver(),
    )
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=orchestrator,
    )
    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)

    lifecycle = asyncio.run(
        _disconnect_while_graph_worker_is_blocked(
            app,
            session_token=login.session_token,
            csrf_token=login.csrf_token,
            model=model,
            factory=database_session_factory,
        )
    )
    assert lifecycle == ("running", True, 0, "interrupted", False, 1)


async def _live_turn_rejects_second_sse_before_started(
    app: Any,
    *,
    session_token: str,
    csrf_token: str,
    model: _BlockingStructuredReplyModel,
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> tuple[tuple[int, bytes], tuple[int, bytes], tuple[int, int, int], tuple[int, int, int]]:
    first_request = asyncio.create_task(
        _direct_asgi_sse_request(
            app,
            session_token=session_token,
            csrf_token=csrf_token,
            content=(
                "申请 insighthub.dashboard_view 14天，"
                "为了 T40 live admission 验收"
            ),
        )
    )
    try:
        worker_started = await asyncio.to_thread(model.started.wait, 5)
        assert worker_started
        before_second = _admission_fact_counts(
            factory,
            workspace_id=workspace_id,
        )
        second_response = await asyncio.wait_for(
            _direct_asgi_sse_request(
                app,
                session_token=session_token,
                csrf_token=csrf_token,
                content="帮助",
            ),
            timeout=5,
        )
        after_second = _admission_fact_counts(
            factory,
            workspace_id=workspace_id,
        )
    finally:
        model.release.set()
    first_response = await asyncio.wait_for(first_request, timeout=5)
    return first_response, second_response, before_second, after_second


def test_live_graph_turn_rejects_second_sse_before_started_or_input_facts(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """P1: live admission returns 409 before any second SSE fact or frame."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    model = _BlockingStructuredReplyModel()
    orchestrator = LangGraphConversationOrchestrator(
        session_factory=database_session_factory,
        workspace_service=WorkspaceService(
            SqlAlchemyWorkspaceStore(database_session_factory)
        ),
        model=model,
        checkpoint_saver=InMemorySaver(),
    )
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=orchestrator,
    )
    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash
                == hash_workspace_token(login.session_token)
            )
        )
        assert workspace is not None
        workspace_id = workspace.id

    first, second, before_second, after_second = asyncio.run(
        _live_turn_rejects_second_sse_before_started(
            app,
            session_token=login.session_token,
            csrf_token=login.csrf_token,
            model=model,
            factory=database_session_factory,
            workspace_id=workspace_id,
        )
    )

    assert second[0] == 409
    assert json.loads(second[1])["detail"]["code"] == "TURN_IN_PROGRESS"
    assert after_second == before_second
    assert first[0] == 200
    first_frames = _parse_sse_frames(first[1].decode())
    assert sum(
        frame["event"]
        in {"message.completed", "error.recoverable", "turn.interrupted"}
        for frame in first_frames
    ) == 1
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id
            )
        )
        assert execution is not None
        assert execution.status == "waiting_input"
        assert execution.lease_expires_at is None
        assert execution.terminal_event_id is not None


def test_expired_turn_rejects_different_sse_input_before_takeover_started(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """P1: mismatched recovery input cannot emit a frame or mutate the turn."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    orchestrator = _langgraph_orchestrator(
        database_session_factory,
        InMemorySaver(),
    )
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=orchestrator,
    )
    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
    assert login.session_id is not None
    original_content = "帮助"
    turn_id = f"turn-t40-expired-{uuid4()}"
    handle = TurnExecutionService(
        database_session_factory,
        lease_seconds=-1,
    ).begin_input(
        workspace_token=login.session_token,
        auth_session_ref=UUID(login.session_id),
        actor_id="EMP-001",
        safe_user_text=original_content,
        input_turn_id=turn_id,
    )
    before_mismatch = _admission_fact_counts(
        database_session_factory,
        workspace_id=handle.workspace_id,
    )

    mismatched = asyncio.run(
        _direct_asgi_sse_request(
            app,
            session_token=login.session_token,
            csrf_token=login.csrf_token,
            content="列出可申请系统",
        )
    )

    assert mismatched[0] == 409
    assert json.loads(mismatched[1])["detail"]["code"] == (
        "TURN_RECOVERY_IN_PROGRESS"
    )
    assert _admission_fact_counts(
        database_session_factory,
        workspace_id=handle.workspace_id,
    ) == before_mismatch
    with database_session_factory() as session:
        untouched = session.get(AgentTurnExecutionRecord, handle.execution_id)
        assert untouched is not None
        assert untouched.status == "running"
        assert untouched.terminal_event_id is None
        assert untouched.attempt == 1
        fence_before_recovery = untouched.lease_fence

    recovered = asyncio.run(
        _direct_asgi_sse_request(
            app,
            session_token=login.session_token,
            csrf_token=login.csrf_token,
            content=original_content,
        )
    )

    assert recovered[0] == 200
    recovered_frames = _parse_sse_frames(recovered[1].decode())
    assert recovered_frames[0]["event"] == "turn.started"
    assert recovered_frames[0]["data"]["turn_id"] == turn_id
    assert recovered_frames[-1]["event"] == "message.completed"
    with database_session_factory() as session:
        execution = session.get(AgentTurnExecutionRecord, handle.execution_id)
        assert execution is not None
        assert execution.status == "completed"
        assert execution.attempt == 2
        assert execution.lease_fence > fence_before_recovery
        assert execution.terminal_event_id is not None
        assert session.scalar(
            select(func.count())
            .select_from(AgentTurnExecutionRecord)
            .where(AgentTurnExecutionRecord.workspace_id == handle.workspace_id)
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == handle.workspace_id,
                WorkspaceEventRecord.event_type == "message.user",
            )
        ) == 1


def test_completion_disconnect_race_reuses_one_fenced_terminal(
    database_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """AC2: completion and disconnect cannot race two terminal owners."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with database_session_factory() as session:
        seed_catalog(session)
    orchestrator = LangGraphConversationOrchestrator(
        session_factory=database_session_factory,
        workspace_service=WorkspaceService(
            SqlAlchemyWorkspaceStore(database_session_factory)
        ),
        model=DeterministicStructuredReplyModel(),
        checkpoint_saver=InMemorySaver(),
    )
    app = create_app(
        settings=_legacy_settings(),
        session_factory=database_session_factory,
        conversation_orchestrator=orchestrator,
    )
    with TestClient(app) as client:
        login = login_as(client, session_factory=database_session_factory)
    assert login.session_id is not None

    turn_id = f"turn-t40-race-{uuid4()}"
    result = orchestrator.prepare(
        workspace_token=login.session_token,
        content="帮助",
        turn_id=turn_id,
        auth_session_id=login.session_id,
    )
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash
                == hash_workspace_token(login.session_token)
            )
        )
        assert workspace is not None
        fence_before = workspace.lease_fence

    first_entered = Event()
    release_first = Event()
    concurrent_entry = Event()
    call_count = 0
    original_finalizer = orchestrator._finalize_deferred_terminal

    def observed_finalizer(**kwargs: Any) -> int | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            concurrent_entry.set()
        return original_finalizer(**kwargs)

    monkeypatch.setattr(
        orchestrator,
        "_finalize_deferred_terminal",
        observed_finalizer,
    )
    outcome = normalized_outcome(result.turn)
    completed_payload = {
        "turn_id": turn_id,
        "message_id": "t40-race-message",
        "content": result.turn.assistant_message,
        **outcome,
    }
    interrupted_payload = {
        "turn_id": turn_id,
        "reason": "client_cancelled",
        "retryable": True,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        completed = executor.submit(
            result.finalize_terminal,
            "message.completed",
            completed_payload,
        )
        assert first_entered.wait(timeout=2)
        interrupted = executor.submit(
            result.finalize_terminal,
            "turn.interrupted",
            interrupted_payload,
        )
        raced = concurrent_entry.wait(timeout=0.2)
        release_first.set()
        event_ids = (completed.result(timeout=2), interrupted.result(timeout=2))

    assert raced is False
    assert event_ids[0] == event_ids[1]
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.input_turn_id == turn_id
            )
        )
        terminal_count = session.scalar(
            select(func.count())
            .select_from(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.event_type.in_(
                    ("message.completed", "error.recoverable", "turn.interrupted")
                ),
                WorkspaceEventRecord.payload["turn_id"].astext == turn_id,
            )
        )
        refreshed_workspace = session.get(WorkspaceRecord, workspace.id)
        assert execution is not None and refreshed_workspace is not None
        assert execution.status == "completed"
        assert execution.accepted_checkpoint_id is not None
        assert execution.lease_expires_at is None
        assert execution.terminal_event_id == event_ids[0]
        assert terminal_count == 1
        assert refreshed_workspace.lease_fence == fence_before


@pytest.mark.parametrize(
    ("pending_entry", "resume_content", "expected_status", "expected_confirmed"),
    [
        ("json", "确认提交", "ready_to_submit", True),
        ("sse", "确认提交", "ready_to_submit", True),
        ("json", "帮助", "answered", False),
        ("sse", "帮助", "answered", False),
    ],
)
def test_real_postgres_product_cross_entry_resume_uses_lifespan_saver(
    pending_entry: str,
    resume_content: str,
    expected_status: str,
    expected_confirmed: bool,
    monkeypatch,
) -> None:
    """AC3: the product app owns the official saver across both entries."""

    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with _isolated_checkpoint_database() as base_settings:
        settings = Settings(
            database_url=base_settings.database_url,
            checkpoint_database_url=base_settings.checkpoint_database_url,
            checkpoint_schema=base_settings.checkpoint_schema,
            orchestrator_mode="mixed",
            langgraph_canary_percent=100,
            demo_mode_enabled=True,
            _env_file=None,
        )
        engine = build_engine(settings.database_url)
        factory = build_session_factory(engine)
        try:
            with factory() as session:
                seed_catalog(session)
                session.commit()
            app = create_app(
                settings=settings,
                session_factory=factory,
                answer_stream_model=DeterministicAnswerStreamModel(),
            )
            with TestClient(app) as client:
                login = login_as(client, session_factory=factory)
                content = (
                    "申请 insighthub.dashboard_view 14天，"
                    f"为了 T40 真实 PG {pending_entry} 跨入口验收"
                )
                if pending_entry == "json":
                    pending_response = client.post(
                        "/api/chat/messages",
                        json={"content": content},
                    )
                    resume_response = client.post(
                        "/api/chat/messages/stream",
                        json={"content": resume_content},
                    )
                    pending_status = pending_response.json()["business_status"]
                    resume_status = next(
                        frame
                        for frame in _parse_sse_frames(resume_response.text)
                        if frame["event"] == "message.completed"
                    )["data"]["payload"]["business_status"]
                else:
                    pending_response = client.post(
                        "/api/chat/messages/stream",
                        json={"content": content},
                    )
                    resume_response = client.post(
                        "/api/chat/messages",
                        json={"content": resume_content},
                    )
                    pending_status = next(
                        frame
                        for frame in _parse_sse_frames(pending_response.text)
                        if frame["event"] == "message.completed"
                    )["data"]["payload"]["business_status"]
                    resume_status = resume_response.json()["business_status"]

            assert pending_response.status_code == 200
            assert resume_response.status_code == 200
            assert pending_status == "awaiting_confirmation"
            assert resume_status == expected_status
            with factory() as session:
                workspace = session.scalar(
                    select(WorkspaceRecord).where(
                        WorkspaceRecord.token_hash
                        == hash_workspace_token(login.session_token)
                    )
                )
                assert workspace is not None and workspace.flow_version == 2
                executions = session.scalars(
                    select(AgentTurnExecutionRecord)
                    .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
                    .order_by(AgentTurnExecutionRecord.input_seq)
                ).all()
                pending = session.scalar(
                    select(AgentPendingInputRecord).where(
                        AgentPendingInputRecord.workspace_id == workspace.id
                    )
                )
                terminal_count = session.scalar(
                    select(func.count())
                    .select_from(WorkspaceEventRecord)
                    .where(
                        WorkspaceEventRecord.workspace_id == workspace.id,
                        WorkspaceEventRecord.event_type.in_(
                            (
                                "message.completed",
                                "error.recoverable",
                                "turn.interrupted",
                            )
                        ),
                    )
                )
            assert len(executions) == 2
            assert executions[0].graph_run_id == executions[1].graph_run_id
            assert executions[0].checkpoint_thread_id == (
                executions[1].checkpoint_thread_id
            )
            assert all(execution.engine == "langgraph" for execution in executions)
            assert all(execution.terminal_event_id is not None for execution in executions)
            assert pending is not None and pending.status == "resolved"
            assert terminal_count == 2
            assert workspace.draft is not None
            assert workspace.draft["confirmed"] is expected_confirmed
            assert (
                WorkspaceService(SqlAlchemyWorkspaceStore(factory))
                .get(login.session_token)
                .active_cursor()
                is None
            )
        finally:
            close_all_sessions()
            engine.dispose()
