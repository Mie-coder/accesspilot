"""Online adapter wired to both engines/transports; all provider output is fake here."""

import json as jsonlib
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.json_orchestrator import LangGraphConversationOrchestrator
from accesspilot.config import Settings
from accesspilot.conversation import DeterministicStructuredReplyModel
from accesspilot.db.models import (
    AccessRequestRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService
from support.auth import login_as


class SemanticProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure: str | None = None
        self.content = "{}"

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any],
             timeout: float) -> "SemanticProvider":
        self.calls.append(json)
        text = json["messages"][-1]["content"]
        is_route = "意图识别器" in json["messages"][0]["content"]
        if is_route and self.failure == "timeout":
            raise httpx.ReadTimeout("private-provider-error")
        if is_route and self.failure == "malformed":
            self.content = '{"intent":"request_access","confirmed":true}'
            return self
        if is_route:
            intent = {
                "我不知道权限编号": "discover_eligible_access",
                "那帮我查询可申请的权限": "discover_eligible_access",
                "先看看有哪些能用的": "discover_eligible_access",
                "今天天气怎么样": "help",
                "含义不明": "unknown",
            }.get(text, "request_access")
            self.content = jsonlib.dumps({"intent": intent})
        else:
            if self.failure == "retry" and not any(
                message.get("content", "").startswith("上一次输出") for message in json["messages"]
            ):
                self.content = "invalid"
                return self
            parsed = {
                "代码仓库只读": {"entitlement_id": "codeforge.repo_read"},
                "申请7天": {"duration_days": 7},
                "7天": {"duration_days": 7},
            }.get(text, {})
            self.content = jsonlib.dumps(parsed)
        return self

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


def semantic_app(
    factory: sessionmaker[Session], engine: str, provider: SemanticProvider, *, online: bool = True,
) -> Any:
    with factory() as session:
        seed_catalog(session)
    model = (
        DeepSeekStructuredReplyModel("test-only", "test-model", client=provider)
        if online else DeterministicStructuredReplyModel()
    )
    policy = PolicyService(embedding_model=DeterministicEmbeddingModel())
    orchestrator = None
    if engine == "langgraph":
        orchestrator = LangGraphConversationOrchestrator(
            session_factory=factory,
            workspace_service=WorkspaceService(SqlAlchemyWorkspaceStore(factory)),
            model=model, policy_service=policy, checkpoint_saver=InMemorySaver(),
        )
    return create_app(
        settings=Settings(_env_file=None), session_factory=factory,
        structured_reply_model=model, policy_service=policy, conversation_orchestrator=orchestrator,
    )


def send(client: TestClient, transport: str, text: str) -> dict[str, Any]:
    suffix = "/stream" if transport == "sse" else ""
    response = client.post("/api/chat/messages" + suffix, json={"content": text})
    assert response.status_code == 200, response.text
    if transport == "json":
        return response.json()
    frames = [
        {"event": next(line[7:] for line in block.splitlines() if line.startswith("event: ")),
         "data": jsonlib.loads(next(
             line[6:] for line in block.splitlines() if line.startswith("data: ")
         ))}
        for block in response.text.split("\n\n") if "event: " in block and "data: " in block
    ]
    terminals = [f for f in frames if f["event"] in {"message.completed", "error.recoverable"}]
    assert len(terminals) == 1, response.text
    return terminals[0]["data"]["payload"]


@pytest.mark.parametrize("engine", ["legacy", "langgraph"])
@pytest.mark.parametrize("transport", ["json", "sse"])
def test_real_conversation_sequence_queries_selects_and_collects_without_submission(
    database_session_factory: sessionmaker[Session], engine: str, transport: str,
) -> None:
    factory = database_session_factory
    provider = SemanticProvider()
    with TestClient(semantic_app(factory, engine, provider)) as client:
        login = login_as(client, session_factory=factory)
        with factory() as session:
            count_before = session.scalar(select(func.count()).select_from(AccessRequestRecord))
        send(client, transport, "我想申请权限")
        before = client.get("/api/drafts/current").json()
        for query in ["我不知道权限编号", "那帮我查询可申请的权限", "先看看有哪些能用的"]:
            result = send(client, transport, query)
            assert result["intent"] == "discover_eligible_access"
            assert "代码仓库只读" in result["assistant_message"]
            assert client.get("/api/drafts/current").json() == before
        calls_before_fields = len(provider.calls)
        selected = send(client, transport, "代码仓库只读")
        assert selected.get("draft", {}).get("entitlement_id") == "codeforge.repo_read", selected
        duration = send(client, transport, "7天")
        assert duration.get("draft", {}).get("duration_days") == 7, duration
        result = send(client, transport, "演示需要")
        assert result["business_status"] == "awaiting_confirmation", result
        draft = client.get("/api/drafts/current").json()["draft"]
        assert draft["entitlement_id"] == "codeforge.repo_read"
        assert draft["justification"] == "演示需要"
        assert draft["duration_days"] == 7
        assert draft["confirmed"] is False
        assert len(provider.calls) == calls_before_fields
        send(client, transport, "不要提交")
        assert client.get("/api/drafts/current").json()["draft"]["confirmed"] is False
        send(client, transport, "取消")
        with factory() as session:
            auth = session.get(AuthSessionRecord, login.session_id)
            assert auth is not None
            workspace = session.get(WorkspaceRecord, auth.workspace_id)
            assert workspace is not None
            assert workspace.cursor_expected_field is None
            assert session.scalar(
                select(func.count()).select_from(AccessRequestRecord)
            ) == count_before
    route_calls = [c for c in provider.calls if "意图识别器" in c["messages"][0]["content"]]
    assert any(c["messages"][-1]["content"] == "我不知道权限编号" for c in route_calls)
    assert not any(c["messages"][-1]["content"] == "演示需要" for c in provider.calls)


@pytest.mark.parametrize("engine", ["legacy", "langgraph"])
@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("failure", ["timeout", "malformed", "unclear"])
def test_unreliable_understanding_never_creates_a_draft(
    database_session_factory: sessionmaker[Session], engine: str, transport: str, failure: str,
) -> None:
    provider = SemanticProvider()
    provider.failure = failure
    with TestClient(semantic_app(database_session_factory, engine, provider)) as client:
        login_as(client)
        before = client.get("/api/drafts/current").json()
        result = send(client, transport, "含义不明" if failure == "unclear" else "申请权限")
        assert result["intent"] == "unknown"
        assert result["business_status"] == "needs_clarification"
        assert "我的权限" in result["assistant_message"]
        assert client.get("/api/drafts/current").json() == before
        assert len(provider.calls) == 1


@pytest.mark.parametrize("engine", ["legacy", "langgraph"])
def test_name_selection_during_reason_question_changes_target_not_justification(
    database_session_factory: sessionmaker[Session], engine: str,
) -> None:
    provider = SemanticProvider()
    with TestClient(semantic_app(database_session_factory, engine, provider)) as client:
        login_as(client)
        send(client, "json", "代码仓库只读")
        send(client, "json", "7天")
        before = len(provider.calls)
        result = send(client, "json", "InsightHub 仪表盘查看")
        assert result["draft"]["entitlement_id"] == "insighthub.dashboard_view"
        assert result["draft"]["justification"] is None
        assert result["draft"]["confirmed"] is False
        assert len(provider.calls) == before
        result = send(client, "json", "今天天气怎么样")
        assert result["intent"] == "help"
        result = send(client, "json", "含义不明")
        assert result["intent"] == "unknown"
        assert result["draft"]["justification"] is None


@pytest.mark.parametrize("engine", ["legacy", "langgraph"])
@pytest.mark.parametrize(("content", "failure", "expected"), [
    ("我能申请什么权限", None, 0),
    ("我不知道权限编号", None, 1),
    ("我想申请权限", None, 2),
    ("我想申请权限", "retry", 3),
    ("我想申请权限", "timeout", 1),
])
def test_safe_events_count_actual_online_attempts_including_retry(
    database_session_factory: sessionmaker[Session], engine: str,
    content: str, failure: str | None, expected: int,
) -> None:
    provider = SemanticProvider()
    provider.failure = failure
    with TestClient(semantic_app(database_session_factory, engine, provider)) as client:
        login = login_as(client, session_factory=database_session_factory)
        send(client, "json", content)
        with database_session_factory() as session:
            auth = session.get(AuthSessionRecord, login.session_id)
            assert auth is not None
            events = session.scalars(select(WorkspaceEventRecord).where(
                WorkspaceEventRecord.workspace_id == auth.workspace_id,
            ).order_by(WorkspaceEventRecord.id)).all()
            started = [e for e in events if e.event_type == "turn.started"]
            assert len(started) == 1 and started[0].payload["model_usage_recorded"] is True
            attempts = [e for e in events if e.event_type == "model.started"
                        and e.payload["provider_mode"] == "api"]
            assert len(attempts) == len(provider.calls) == expected
            assert len({(e.payload["step_id"], e.payload["attempt"]) for e in attempts}) == expected
            completed = [e for e in events if e.event_type == "model.completed"]
            assert len(completed) == expected
            if failure == "timeout":
                assert completed[0].payload["status"] == "unavailable"
            if failure == "retry":
                assert [e.payload["attempt"] for e in attempts] == [1, 1, 2]
                assert "malformed" in [e.payload["status"] for e in completed]
            assert all("test-only" not in str(e.payload) for e in events)


@pytest.mark.parametrize("engine", ["legacy", "langgraph"])
def test_no_key_product_route_returns_explicit_fallback_without_a_draft(
    database_session_factory: sessionmaker[Session], engine: str,
) -> None:
    with TestClient(semantic_app(
        database_session_factory, engine, SemanticProvider(), online=False,
    )) as client:
        login_as(client)
        for text in ("我不知道权限编号", "那帮我查询可申请的权限"):
            result = send(client, "json", text)
            assert result["intent"] == "unknown"
            assert "理解服务" in result["assistant_message"]
            assert client.get("/api/drafts/current").json()["draft"] is None
