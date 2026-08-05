from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import (
    DeterministicEmbeddingModel,
    EmbeddingProviderError,
)
from accesspilot.db.models import (
    AccessRequestRecord,
    AuditEventRecord,
    PolicyChunkRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.rag.policies import index_policy_embeddings
from accesspilot.risk.review import (
    MalformedRiskReviewError,
    PolicyCitation,
    RiskReview,
    RiskReviewContext,
    RiskReviewFailed,
    review_request_risk,
    validate_risk_review_json,
)


class StaticRiskReviewModel:
    def __init__(self, review: RiskReview) -> None:
        self._review = review
        self.last_context: RiskReviewContext | None = None

    def review(self, context: RiskReviewContext) -> RiskReview:
        self.last_context = context
        return self._review


class FailingEmbeddingModel:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingProviderError("向量服务暂时不可用")


class FailingRiskReviewModel:
    def review(self, context: RiskReviewContext) -> RiskReview:
        raise MalformedRiskReviewError("模型格式错误")


def create_request(session: Session) -> UUID:
    seed_catalog(session)
    workspace = WorkspaceRecord(token_hash=sha256(uuid4().bytes).hexdigest())
    session.add(workspace)
    session.flush()
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id="EMP-001",
        entitlement_code="insighthub.customer_export",
        duration_days=14,
        justification="核验虚构项目运营数据",
        request_status="submitted",
        confirmed_at=datetime.now(UTC),
    )
    session.add(request)
    session.commit()
    return request.id


def prepare_policy_index(session: Session) -> DeterministicEmbeddingModel:
    model = DeterministicEmbeddingModel()
    index_policy_embeddings(session, model)
    return model


def clear_policy_embeddings(session: Session) -> None:
    for chunk in session.scalars(select(PolicyChunkRecord)).all():
        chunk.embedding = None
    session.commit()


def valid_review(*, policy_code: str = "POL-003") -> RiskReview:
    return RiskReview(
        risk_level="high",
        outcome="requires_human_review",
        summary="高风险导出权限需要两级人工审批。",
        findings=["申请期限为 14 天", "不得跳过人工审批"],
        citations=[
            PolicyCitation(
                policy_code=policy_code,
                reason="该条款要求经理和数据所有者依次审批。",
            )
        ],
    )


def test_risk_review_is_strict_and_rejects_unknown_fields() -> None:
    with pytest.raises(MalformedRiskReviewError):
        validate_risk_review_json(
            """{
                "risk_level": "high",
                "outcome": "requires_human_review",
                "summary": "需要人工审批",
                "findings": [],
                "citations": [],
                "approved": true
            }"""
        )


def test_review_reads_request_and_policies_without_mutating_business_state(
    database_session: Session,
) -> None:
    request_id = create_request(database_session)
    embedding_model = prepare_policy_index(database_session)
    model = StaticRiskReviewModel(valid_review())
    audit_count_before = database_session.scalar(
        select(func.count()).select_from(AuditEventRecord)
    )

    result = review_request_risk(
        database_session,
        request_id=request_id,
        embedding_model=embedding_model,
        review_model=model,
    )

    stored_request = database_session.get(AccessRequestRecord, request_id)
    audit_count_after = database_session.scalar(
        select(func.count()).select_from(AuditEventRecord)
    )
    assert result == valid_review()
    assert model.last_context is not None
    assert len(model.last_context.policies) == 4
    assert stored_request is not None
    assert stored_request.request_status == "submitted"
    assert audit_count_after == audit_count_before
    assert not database_session.new
    assert not database_session.dirty
    assert not database_session.deleted

    clear_policy_embeddings(database_session)


def test_review_rejects_policy_code_not_returned_by_retrieval(
    database_session: Session,
) -> None:
    request_id = create_request(database_session)
    embedding_model = prepare_policy_index(database_session)

    with pytest.raises(RiskReviewFailed):
        review_request_risk(
            database_session,
            request_id=request_id,
            embedding_model=embedding_model,
            review_model=StaticRiskReviewModel(valid_review(policy_code="POL-999")),
        )

    clear_policy_embeddings(database_session)


def test_review_rejects_model_attempt_to_downgrade_catalog_risk(
    database_session: Session,
) -> None:
    request_id = create_request(database_session)
    embedding_model = prepare_policy_index(database_session)
    unsafe_review = RiskReview(
        risk_level="low",
        outcome="clear",
        summary="模型错误地判断为低风险。",
        findings=["无需人工处理"],
        citations=[
            PolicyCitation(
                policy_code="POL-003",
                reason="错误引用不能覆盖目录风险等级。",
            )
        ],
    )

    with pytest.raises(RiskReviewFailed):
        review_request_risk(
            database_session,
            request_id=request_id,
            embedding_model=embedding_model,
            review_model=StaticRiskReviewModel(unsafe_review),
        )

    clear_policy_embeddings(database_session)


@pytest.mark.parametrize(
    ("embedding_model", "review_model"),
    [
        (FailingEmbeddingModel(), StaticRiskReviewModel(valid_review())),
        (DeterministicEmbeddingModel(), FailingRiskReviewModel()),
    ],
)
def test_provider_failure_becomes_recoverable_without_fabricated_review(
    database_session: Session,
    embedding_model: object,
    review_model: object,
) -> None:
    request_id = create_request(database_session)
    if isinstance(embedding_model, DeterministicEmbeddingModel):
        index_policy_embeddings(database_session, embedding_model)

    with pytest.raises(RiskReviewFailed):
        review_request_risk(
            database_session,
            request_id=request_id,
            embedding_model=embedding_model,  # type: ignore[arg-type]
            review_model=review_model,  # type: ignore[arg-type]
        )

    stored_request = database_session.get(AccessRequestRecord, request_id)
    assert stored_request is not None
    assert stored_request.request_status == "submitted"

    clear_policy_embeddings(database_session)
