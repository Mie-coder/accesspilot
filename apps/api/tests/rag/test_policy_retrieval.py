import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import (
    DeterministicEmbeddingModel,
    EmbeddingModel,
)
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.rag.policies import (
    PolicyEmbeddingError,
    index_policy_embeddings,
    search_policies,
)


class WrongDimensionEmbeddingModel:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 511 for _ in texts]


def clear_policy_embeddings(session: Session) -> None:
    for chunk in session.scalars(select(PolicyChunkRecord)).all():
        chunk.embedding = None
    session.commit()


def test_indexes_all_original_policies_as_512_dimensions(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    clear_policy_embeddings(database_session)

    count = index_policy_embeddings(
        database_session,
        DeterministicEmbeddingModel(),
    )

    chunks = database_session.scalars(
        select(PolicyChunkRecord).order_by(PolicyChunkRecord.policy_code)
    ).all()
    assert count == 8
    assert len(chunks) == 8
    assert all(chunk.embedding is not None for chunk in chunks)
    assert all(len(chunk.embedding or []) == 512 for chunk in chunks)

    clear_policy_embeddings(database_session)


def test_pgvector_returns_four_stable_policy_references(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    clear_policy_embeddings(database_session)
    model: EmbeddingModel = DeterministicEmbeddingModel()
    index_policy_embeddings(database_session, model)

    matches = search_policies(
        database_session,
        "客户数据导出权限申请 14 天，需要经理和数据所有者审批",
        model,
    )

    assert len(matches) == 4
    assert len({match.policy_code for match in matches}) == 4
    assert "POL-003" in {match.policy_code for match in matches}
    assert "POL-004" in {match.policy_code for match in matches}
    assert all(0.0 <= match.similarity <= 1.0 for match in matches)

    clear_policy_embeddings(database_session)


def test_wrong_dimension_rolls_back_without_partial_policy_vectors(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    clear_policy_embeddings(database_session)

    with pytest.raises(PolicyEmbeddingError):
        index_policy_embeddings(database_session, WrongDimensionEmbeddingModel())

    embeddings = database_session.scalars(
        select(PolicyChunkRecord.embedding)
    ).all()
    assert all(embedding is None for embedding in embeddings)
