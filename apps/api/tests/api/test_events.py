import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.domain.models import ParsedReply
from accesspilot.events import UnsafeEventError, append_workspace_event
from accesspilot.main import create_app


class StaticStructuredReplyModel:
    def __init__(self) -> None:
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        self.calls += 1
        return ParsedReply(employee_id="EMP-001")


def test_sse_reconnect_only_replays_events_after_last_event_id(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
        )
    )
    assert client.post("/api/workspaces").status_code == 201
    token = client.cookies.get("accesspilot_workspace")
    assert token is not None
    with database_session_factory() as session:
        first = append_workspace_event(
            session,
            workspace_token=token,
            event_type="message.user",
            payload={"content": "第一条"},
        )
        second = append_workspace_event(
            session,
            workspace_token=token,
            event_type="message.assistant",
            payload={"content": "第二条"},
        )
        third = append_workspace_event(
            session,
            workspace_token=token,
            event_type="business.status",
            payload={"status": "collecting"},
        )

    response = client.get(
        "/api/events",
        headers={"Last-Event-ID": str(first.id)},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {first.id}\n" not in response.text
    assert f"id: {second.id}\n" in response.text
    assert f"id: {third.id}\n" in response.text
    assert "隐藏推理" not in response.text


def test_sse_rejects_invalid_last_event_id(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
        )
    )
    client.post("/api/workspaces")

    response = client.get(
        "/api/events",
        headers={"Last-Event-ID": "not-an-integer"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Last-Event-ID 必须是非负整数"}


def test_chat_api_returns_429_after_quota_and_history_remains_available(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = StaticStructuredReplyModel()
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            structured_reply_model=model,
        )
    )
    client.post("/api/workspaces")
    assert client.post("/api/demo/session", json={"employee_id": "EMP-001"}).status_code == 200
    token = client.cookies.get("accesspilot_workspace")
    assert token is not None
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.token_hash == hash_workspace_token(token))
        )
        assert workspace is not None
        workspace.model_call_limit = 1
        session.commit()

    first = client.post("/api/chat/messages", json={"content": "我是 EMP-001"})
    exhausted = client.post("/api/chat/messages", json={"content": "申请 7 天"})
    history = client.get("/api/events")
    quota = client.get("/api/demo/model-quota")

    assert first.status_code == 200
    assert exhausted.status_code == 429
    assert exhausted.json() == {"detail": "模型调用额度已用尽，当前为只读回放模式"}
    assert history.status_code == 200
    assert "我是 EMP-001" in history.text
    assert "申请 7 天" not in history.text
    assert quota.json() == {"used": 1, "limit": 1, "remaining": 0, "retry_consumed": 0}
    assert model.calls == 1


def test_chat_api_rejects_oversized_message_before_consuming_quota(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = StaticStructuredReplyModel()
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            structured_reply_model=model,
        )
    )
    client.post("/api/workspaces")

    response = client.post("/api/chat/messages", json={"content": "x" * 10_001})
    assert client.post("/api/demo/session", json={"employee_id": "EMP-001"}).status_code == 200
    quota = client.get("/api/demo/model-quota")
    history = client.get("/api/events")

    assert response.status_code == 422
    assert quota.json() == {"used": 0, "limit": 20, "remaining": 20, "retry_consumed": 0}
    assert history.text == ""
    assert model.calls == 0


def test_event_payload_rejects_sensitive_string_values(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
        )
    )
    client.post("/api/workspaces")
    token = client.cookies.get("accesspilot_workspace")
    assert token is not None

    with database_session_factory() as session:
        with pytest.raises(UnsafeEventError):
            append_workspace_event(
                session,
                workspace_token=token,
                event_type="draft.updated",
                payload={
                    "draft": {"justification": "API_KEY=sk-secret-123456"},
                    "missing_fields": [],
                    "can_enter_approval": False,
                },
            )
