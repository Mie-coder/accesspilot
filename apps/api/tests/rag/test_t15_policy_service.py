"""T15 政策事实源、RAG 三态与自审批规则红测。"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.rag.policies import PolicyEmbeddingError
from accesspilot.tools.policies import (
    PolicyAnswer,
    PolicyCatalogUnavailableError,
    PolicyService,
)


def _service(session: Session, *, threshold: float = 0.20) -> PolicyService:
    return PolicyService(
        embedding_model=DeterministicEmbeddingModel(),
        similarity_threshold=threshold,
    )


def _index(session: Session) -> None:
    service = _service(session)
    assert service.index(session) == 8


def test_policy_catalog_returns_all_eight_fact_source_records(
    database_session: Session,
) -> None:
    seed_catalog(database_session)

    catalog = _service(database_session).catalog(database_session)

    assert len(catalog) == 8
    assert [item.policy_code for item in catalog] == [
        "POL-001",
        "POL-002",
        "POL-003",
        "POL-004",
        "POL-005",
        "POL-006",
        "POL-007",
        "POL-008",
    ]
    assert all(item.version == "v1" for item in catalog)
    assert all(item.source == "fictional_access_policy" for item in catalog)
    assert all(item.similarity is None for item in catalog)


def test_policy_query_is_grounded_only_in_current_matches(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    _index(database_session)

    answer = _service(database_session).query(
        database_session,
        "客户数据导出权限申请需要哪些审批？",
    )

    assert isinstance(answer, PolicyAnswer)
    assert answer.status == "grounded"
    assert answer.evidence
    assert all(item.similarity >= 0.20 for item in answer.evidence)
    assert all(
        set(item.model_dump())
        >= {"policy_code", "title", "content", "version", "source", "similarity"}
        for item in answer.evidence
    )


def test_low_similarity_returns_insufficient_without_policy_body(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    _index(database_session)

    answer = _service(database_session).query(
        database_session,
        "今天天气如何",
    )

    assert answer.status == "insufficient_evidence"
    assert answer.evidence == []


def test_partial_index_maps_to_retrieval_unavailable(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    chunks = database_session.scalars(select(PolicyChunkRecord)).all()
    for chunk in chunks:
        chunk.embedding = None
    chunks[0].embedding = [0.0] * 512
    database_session.commit()

    answer = _service(database_session).query(database_session, "审批政策")

    assert answer.status == "retrieval_unavailable"
    assert answer.evidence == []


def test_missing_policy_fails_catalog_index_and_query_closed(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    _index(database_session)
    missing = database_session.scalar(
        select(PolicyChunkRecord).where(
            PolicyChunkRecord.policy_code == "POL-008"
        )
    )
    assert missing is not None
    database_session.delete(missing)
    database_session.commit()

    service = _service(database_session)
    with pytest.raises(PolicyCatalogUnavailableError):
        service.catalog(database_session)
    with pytest.raises(PolicyEmbeddingError):
        service.index(database_session)
    answer = service.query(database_session, "审批政策")

    assert answer.status == "retrieval_unavailable"
    assert answer.evidence == []


def test_self_approval_always_forbidden_and_only_cites_pol006(
    database_session: Session,
) -> None:
    seed_catalog(database_session)

    answer = _service(database_session).query(
        database_session,
        "我可以自己审批自己的权限吗？",
    )

    assert answer.status == "grounded"
    assert "禁止" in answer.answer or "不能" in answer.answer
    assert [item.policy_code for item in answer.evidence] == ["POL-006"]


def test_embedding_or_database_failure_is_safe_business_status(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_catalog(database_session)
    service = _service(database_session)

    def explode(*args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        raise RuntimeError("provider key and SQL must not escape")

    monkeypatch.setattr("accesspilot.tools.policies.search_policies", explode)

    answer = service.query(database_session, "政策问题")

    assert answer.status == "retrieval_unavailable"
    assert "provider key" not in answer.answer
    assert answer.evidence == []
