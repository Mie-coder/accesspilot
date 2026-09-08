"""Catalog-grounded extraction through both supported conversation engines."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.json_orchestrator import LangGraphConversationOrchestrator
from accesspilot.config import Settings
from accesspilot.db.models import AuthSessionRecord, WorkspaceEventRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService
from support.auth import login_as


class GroundedHttpClient:
    def __init__(self, entitlement: str) -> None:
        self.entitlement = entitlement
        self.messages: list[dict[str, str]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any],
             timeout: float) -> "GroundedHttpClient":
        self.messages = json["messages"]
        return self

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        if "意图识别器" in self.messages[0]["content"]:
            return {"choices": [{"message": {"content": '{"intent":"request_access"}'}}]}
        return {"choices": [{"message": {"content": json.dumps({
            "entitlement_id": self.entitlement, "confirmed": True,
        })}}]}


@pytest.mark.parametrize("engine", ["legacy", "langgraph"])
@pytest.mark.parametrize(("target", "expected"), [
    ("codeforge.repo_read", "collecting"),
    ("数据洞察中心", "entitlement_ambiguous"),
    ("codeforge.repo_write", "entitlement_no_match"),
])
def test_context_is_scoped_and_model_candidates_still_require_catalog_validation(
    database_session_factory: sessionmaker[Session], engine: str, target: str, expected: str,
) -> None:
    factory = database_session_factory
    with factory() as session:
        seed_catalog(session)
    client = GroundedHttpClient(target)
    model = DeepSeekStructuredReplyModel(
        api_key="test-only", model_name="test-model", client=client,
    )
    policy = PolicyService(embedding_model=DeterministicEmbeddingModel())
    orchestrator = None
    if engine == "langgraph":
        orchestrator = LangGraphConversationOrchestrator(
            session_factory=factory,
            workspace_service=WorkspaceService(SqlAlchemyWorkspaceStore(factory)),
            model=model, policy_service=policy, checkpoint_saver=InMemorySaver(),
        )
    app = create_app(
        settings=Settings(_env_file=None), session_factory=factory,
        structured_reply_model=model, policy_service=policy,
        conversation_orchestrator=orchestrator,
    )
    with TestClient(app) as browser:
        other = login_as(browser, "EMP-002", session_factory=factory)
        with factory() as session:
            auth = session.get(AuthSessionRecord, other.session_id)
            assert auth is not None
            session.add(WorkspaceEventRecord(
                workspace_id=auth.workspace_id, event_type="message.user",
                payload={"content": "other-workspace-canary"},
            ))
            session.commit()
        own = login_as(browser, "EMP-001", session_factory=factory)
        with factory() as session:
            auth = session.get(AuthSessionRecord, own.session_id)
            assert auth is not None
            session.add(WorkspaceEventRecord(
                workspace_id=auth.workspace_id, event_type="message.user",
                payload={"content": "我想看代码 password=fake-secret-value"},
            ))
            session.commit()
        response = browser.post("/api/chat/messages", json={"content": "那申请一下代码仓库的吧"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["business_status"] == expected
        if expected == "collecting":
            assert payload["draft"]["confirmed"] is False
            assert payload["draft"]["duration_days"] is None
            assert payload["draft"]["justification"] is None
            assert payload["draft"]["entitlement_id"] == "codeforge.repo_read"
        else:
            assert payload["draft"] is None
            assert payload["draft_revision"] == 0
        context = client.messages[1]["content"]
        assert "代码仓库只读" in context
        assert "我想看代码" in context
        assert "other-workspace-canary" not in context
        assert "fake-secret-value" not in context
        assert "opsdesk.log_view" not in context
        # Context must not become an event/trace containing provider configuration.
        with factory() as session:
            events = session.scalars(select(WorkspaceEventRecord).where(
                WorkspaceEventRecord.workspace_id == auth.workspace_id,
            )).all()
            assert all("test-only" not in str(event.payload) for event in events)
