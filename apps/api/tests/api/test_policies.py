"""T15 政策 HTTP API 红测。

政策目录和检索结果都是类型化响应；三种检索状态均为业务成功响应，
而不是把证据不足或索引故障伪装成普通文本或 HTTP 500。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.config import Settings
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app
from accesspilot.rag.policies import index_policy_embeddings


class RecordingEmbeddingModel:
    """保留确定性向量行为，同时记录送往向量供应商的原文。"""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self._delegate = DeterministicEmbeddingModel()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return self._delegate.embed(texts)


def _client(
    factory: sessionmaker[Session],
    *,
    indexed: bool = False,
    embedding_model: RecordingEmbeddingModel | None = None,
) -> TestClient:
    with factory() as session:
        seed_catalog(session)
        if not indexed:
            for chunk in session.scalars(select(PolicyChunkRecord)).all():
                chunk.embedding = None
            session.commit()
        else:
            assert index_policy_embeddings(session, DeterministicEmbeddingModel()) == 8
    return TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=False),
            store=SqlAlchemyWorkspaceStore(factory),
            session_factory=factory,
            # API 合同测试必须与本机 .env 隔离，不消耗真实百炼额度。
            embedding_model=embedding_model or DeterministicEmbeddingModel(),
        )
    )


def test_policy_catalog_api_returns_all_eight_records(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory)

    response = client.get("/api/policies")

    assert response.status_code == 200
    assert len(response.json()["policies"]) == 8
    assert response.json()["policies"][0]["policy_code"] == "POL-001"


def test_policy_query_api_returns_typed_business_statuses(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory, indexed=False)

    response = client.post("/api/policies/query", json={"query": "自审批规则"})

    assert response.status_code == 200
    assert response.json()["status"] == "grounded"
    assert [item["policy_code"] for item in response.json()["evidence"]] == ["POL-006"]


def test_policy_query_api_returns_grounded_evidence_from_index(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory, indexed=True)

    response = client.post(
        "/api/policies/query",
        json={"query": "客户数据导出权限需要哪些审批？"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "grounded"
    assert payload["evidence"]
    assert all(
        set(item)
        >= {"policy_code", "title", "content", "version", "source", "similarity"}
        for item in payload["evidence"]
    )


def test_policy_query_api_returns_insufficient_evidence_without_500(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory, indexed=True)

    response = client.post(
        "/api/policies/query",
        json={"query": "天气和午餐与权限无关吗？"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_evidence"
    assert response.json()["evidence"] == []


def test_policy_query_api_returns_retrieval_unavailable_without_500(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory, indexed=False)

    response = client.post(
        "/api/policies/query",
        json={"query": "客户数据导出权限需要哪些审批？"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "retrieval_unavailable"
    assert response.json()["evidence"] == []


def test_policy_query_api_rejects_unknown_body_fields(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory)

    response = client.post(
        "/api/policies/query",
        json={"query": "政策", "employee_id": "EMP-003"},
    )

    assert response.status_code == 422


def test_policy_query_api_rejects_blank_query(
    database_session_factory: sessionmaker[Session],
) -> None:
    client = _client(database_session_factory)

    response = client.post("/api/policies/query", json={"query": "   "})

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("credential", "secret"),
    [
        ("API_KEY=sk-demo-secret-123456", "sk-demo-secret-123456"),
        ("client_secret=plainsecret123", "plainsecret123"),
    ],
)
def test_policy_api_redacts_credentials_before_embedding_and_answer(
    database_session_factory: sessionmaker[Session],
    credential: str,
    secret: str,
) -> None:
    recorder = RecordingEmbeddingModel()
    client = _client(
        database_session_factory,
        indexed=True,
        embedding_model=recorder,
    )
    response = client.post(
        "/api/policies/query",
        json={"query": f"客户数据导出政策，{credential}"},
    )

    assert response.status_code == 200
    assert secret not in str(recorder.seen)
    assert secret not in response.text
