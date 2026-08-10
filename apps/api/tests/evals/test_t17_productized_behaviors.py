"""T17 fixed behaviors that intentionally span more than one earlier Ticket."""

from hashlib import sha256

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import ParsedReply
from accesspilot.main import create_app


class FailIfCalledStructuredReplyModel:
    """Quota protection must reject new extraction before invoking this model."""

    def __init__(self) -> None:
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        raise AssertionError("model must not run after budget exhaustion")


def test_budget_exhaustion_keeps_read_only_facts_and_confirmed_submit_available(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        seed_catalog(session)
    model = FailIfCalledStructuredReplyModel()
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            structured_reply_model=model,
        )
    )
    assert client.post("/api/workspaces").status_code == 201
    preview = client.post(
        "/api/drafts/preview",
        json={
            "employee_id": "EMP-001",
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验虚构季度客户分析数据",
            "confirmed": True,
        },
    )
    assert preview.status_code == 200
    original_draft = preview.json()["draft"]

    token = client.cookies.get("accesspilot_workspace")
    assert token is not None
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == sha256(token.encode()).hexdigest()
            )
        )
        assert workspace is not None
        workspace.model_call_limit = 1
        workspace.model_calls_used = 1
        session.commit()

    rejected = client.post(
        "/api/chat/messages",
        json={"content": "再申请一个新的权限并从自然语言提取字段"},
    )
    current_draft = client.get("/api/drafts/current")
    overview = client.get("/api/access-overview")
    history = client.get("/api/events?follow=false")
    submitted = client.post("/api/requests")

    assert rejected.status_code == 429
    assert current_draft.status_code == 200
    assert current_draft.json()["draft"] == original_draft
    assert overview.status_code == 200
    assert overview.json()["items"]
    assert history.status_code == 200
    assert submitted.status_code == 201
    assert model.calls == 0

    with database_session_factory() as session:
        persisted = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == sha256(token.encode()).hexdigest()
            )
        )
        assert persisted is not None
        assert persisted.model_calls_used == 1
