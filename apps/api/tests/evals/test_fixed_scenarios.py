"""T19 当前仍保持原语义的八条 T08 API 评测。"""
import re
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.structured_reply import MalformedStructuredOutputError
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    PolicyChunkRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore, hash_workspace_token
from accesspilot.domain.models import ParsedReply
from accesspilot.main import create_app
from accesspilot.provisioning import IamOutcome, SimulatedIamProvisioner
from accesspilot.rag.policies import index_policy_embeddings
from accesspilot.risk.review import DeterministicRiskReviewModel
from support.auth import login_as


class FailingEmbeddingModel:
    """固定模拟政策向量服务不可用。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("fixed evaluation embedding failure")


class AlwaysMalformedReplyModel:
    """固定模拟模型连续两次返回不符合 Schema 的内容。"""

    def __init__(self) -> None:
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        self.calls += 1
        raise MalformedStructuredOutputError("fixed malformed output")


class CountingIamProvisioner(SimulatedIamProvisioner):
    """记录真实 provision 边界调用次数，证明重放不会重复触发 IAM。"""

    def __init__(self) -> None:
        super().__init__()
        self.provision_calls = 0

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> IamOutcome:
        self.provision_calls += 1
        return super().provision(
            request_id=request_id,
            idempotency_key=idempotency_key,
            fault_mode=fault_mode,
        )


class SequencedIamProvisioner:
    """Return fixed provision/query outcomes and record external boundary calls."""

    def __init__(
        self,
        *,
        provision_outcomes: list[IamOutcome],
        query_outcomes: list[IamOutcome],
    ) -> None:
        self.provision_outcomes = provision_outcomes
        self.query_outcomes = query_outcomes
        self.provision_calls: list[str] = []
        self.query_calls: list[str] = []

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> IamOutcome:
        self.provision_calls.append(idempotency_key)
        return self.provision_outcomes.pop(0)

    def query_status(self, *, idempotency_key: str) -> IamOutcome:
        self.query_calls.append(idempotency_key)
        return self.query_outcomes.pop(0)


@pytest.fixture
def eval_iam() -> CountingIamProvisioner:
    return CountingIamProvisioner()


@pytest.fixture
def eval_client(
    database_session_factory: sessionmaker[Session],
    eval_iam: CountingIamProvisioner,
) -> Iterator[TestClient]:
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            embedding_model=embedding_model,
            risk_review_model=DeterministicRiskReviewModel(),
            iam_provisioner=eval_iam,
        )
    )
    login_as(client)
    yield client
    with database_session_factory() as session:
        for chunk in session.scalars(select(PolicyChunkRecord)).all():
            chunk.embedding = None
        session.commit()


def create_workspace(client: TestClient) -> None:
    login_as(client)


def save_draft(client: TestClient, *, confirmed: bool) -> None:
    response = client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": "核验虚构季度客户分析数据",
            "confirmed": confirmed,
        },
    )
    assert response.status_code == 200


def submit_request(client: TestClient, *, confirmed: bool = True) -> str:
    create_workspace(client)
    save_draft(client, confirmed=confirmed)
    response = client.post("/api/requests")
    assert response.status_code == 201
    return response.json()["request_id"]


def start_case(client: TestClient, request_id: str) -> str:
    response = client.post(f"/api/requests/{request_id}/approval-case")
    assert response.status_code == 201
    return response.json()["approval_case_id"]


def preset_approved_case(
    database_session_factory: sessionmaker[Session],
    request_id: str,
) -> None:
    """Prepare an approved DB fact without pretending T20/T22 APIs exist."""

    with database_session_factory() as session:
        case = session.scalar(
            select(ApprovalCaseRecord).where(
                ApprovalCaseRecord.request_id == UUID(request_id)
            )
        )
        assert case is not None
        case.approval_status = "approved"
        steps = session.scalars(
            select(ApprovalStepRecord).where(
                ApprovalStepRecord.approval_case_id == case.id
            )
        ).all()
        assert steps
        for step in steps:
            step.step_status = "approved"
        session.commit()


def count_grants(
    database_session_factory: sessionmaker[Session],
    request_id: str,
) -> int:
    with database_session_factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(AccessGrantRecord)
            .where(AccessGrantRecord.request_id == UUID(request_id))
        ) or 0


def test_eval_02_missing_fields_remain_a_draft(eval_client: TestClient) -> None:
    create_workspace(eval_client)
    preview = eval_client.post(
        "/api/drafts/preview",
        json={"confirmed": False},
    )
    submitted = eval_client.post("/api/requests")

    assert preview.json()["is_complete"] is False
    assert preview.json()["missing_fields"] == [
        "entitlement_id",
        "duration_days",
        "justification",
    ]
    assert submitted.status_code == 409


def test_eval_03_complete_but_unconfirmed_cannot_submit(
    eval_client: TestClient,
) -> None:
    create_workspace(eval_client)
    save_draft(eval_client, confirmed=False)

    response = eval_client.post("/api/requests")

    assert response.status_code == 409
    assert response.json() == {"detail": "申请草稿尚未明确确认"}


def test_eval_04_policy_failure_is_recoverable_without_approval(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        seed_catalog(session)
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            embedding_model=FailingEmbeddingModel(),
            risk_review_model=DeterministicRiskReviewModel(),
        )
    )
    request_id = submit_request(client)

    response = client.post(f"/api/requests/{request_id}/approval-case")
    detail = client.get(f"/api/requests/{request_id}")

    assert response.status_code == 503
    assert response.json() == {"detail": "风险审查暂时不可用，请稍后重试"}
    assert detail.json()["approval"] is None


def test_eval_07_timeout_recovers_by_querying_original_operation(
    database_session_factory: sessionmaker[Session],
) -> None:
    iam = SequencedIamProvisioner(
        provision_outcomes=[IamOutcome(status="unknown", message="IAM 响应超时，结果未知")],
        query_outcomes=[IamOutcome(status="succeeded")],
    )
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            embedding_model=embedding_model,
            risk_review_model=DeterministicRiskReviewModel(),
            iam_provisioner=iam,
        )
    )
    login_as(client)
    request_id = submit_request(client)
    start_case(client, request_id)
    preset_approved_case(database_session_factory, request_id)
    key = f"eval-timeout-{request_id}"

    unknown = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )
    replay = client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )
    recovered = client.post(f"/api/requests/{request_id}/provision/recover")
    recovered_again = client.post(f"/api/requests/{request_id}/provision/recover")

    assert unknown.status_code == 200
    assert unknown.json()["provisioning_status"] == "unknown"
    assert unknown.json()["access_granted"] is False
    assert replay.json()["provisioning_attempt_id"] == unknown.json()[
        "provisioning_attempt_id"
    ]
    assert recovered.json()["provisioning_status"] == "succeeded"
    assert recovered_again.json()["provisioning_attempt_id"] == recovered.json()[
        "provisioning_attempt_id"
    ]
    assert iam.provision_calls == [key]
    assert iam.query_calls == [key]
    assert count_grants(database_session_factory, request_id) == 1


def test_eval_08_duplicate_retry_reuses_one_attempt_and_grant(
    eval_client: TestClient,
    eval_iam: CountingIamProvisioner,
    database_session_factory: sessionmaker[Session],
) -> None:
    request_id = submit_request(eval_client)
    start_case(eval_client, request_id)
    preset_approved_case(database_session_factory, request_id)
    key = f"eval-idempotent-{request_id}"

    first = eval_client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )
    second = eval_client.post(
        f"/api/requests/{request_id}/provision",
        json={"idempotency_key": key},
    )

    assert first.status_code == 200
    assert first.json()["provisioning_status"] == "succeeded"
    assert first.json()["provisioning_attempt_id"] == second.json()[
        "provisioning_attempt_id"
    ]
    assert first.json()["attempt_count"] == second.json()["attempt_count"] == 1
    assert eval_iam.provision_calls == 1
    assert count_grants(database_session_factory, request_id) == 1


def test_eval_09_sse_reconnect_only_replays_newer_events(
    eval_client: TestClient,
) -> None:
    create_workspace(eval_client)
    assert eval_client.post(
        "/api/chat/messages", json={"content": "我是 EMP-001"}
    ).status_code == 200
    assert eval_client.post(
        "/api/chat/messages", json={"content": "申请 14 天"}
    ).status_code == 200
    history = eval_client.get("/api/events?follow=false")
    event_ids = [int(value) for value in re.findall(r"^id: (\d+)$", history.text, re.M)]
    cursor = event_ids[len(event_ids) // 2]

    replayed = eval_client.get(
        "/api/events?follow=false",
        headers={"Last-Event-ID": str(cursor)},
    )
    replayed_ids = [
        int(value) for value in re.findall(r"^id: (\d+)$", replayed.text, re.M)
    ]

    assert event_ids
    assert replayed_ids
    assert all(event_id > cursor for event_id in replayed_ids)


def test_eval_10_quota_exhaustion_keeps_history_readable(
    eval_client: TestClient,
    database_session_factory: sessionmaker[Session],
) -> None:
    create_workspace(eval_client)
    token = eval_client.cookies.get("accesspilot_session")
    assert token is not None
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(token)
            )
        )
        assert workspace is not None
        workspace.model_call_limit = 1
        session.commit()

    first = eval_client.post("/api/chat/messages", json={"content": "我是 EMP-001"})
    exhausted = eval_client.post("/api/chat/messages", json={"content": "申请 7 天"})
    history = eval_client.get("/api/events?follow=false")

    assert first.status_code == 200
    assert exhausted.status_code == 429
    assert history.status_code == 200
    assert "我是 EMP-001" in history.text
    assert "申请 7 天" not in history.text


def test_eval_12_two_malformed_replies_fail_closed(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = AlwaysMalformedReplyModel()
    client = TestClient(
        create_app(
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            settings=Settings(demo_mode_enabled=True),
            session_factory=database_session_factory,
            structured_reply_model=model,
        )
    )
    create_workspace(client)

    response = client.post("/api/chat/messages", json={"content": "帮我申请权限"})
    history = client.get("/api/events?follow=false")

    assert response.status_code == 200
    assert response.json()["business_status"] == "recoverable_error"
    assert response.json()["draft"]["confirmed"] is False
    assert model.calls == 2
    assert "MODEL_REPLY_UNAVAILABLE" in history.text
