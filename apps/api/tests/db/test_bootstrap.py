from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import EMBEDDING_DIMENSIONS, DeterministicEmbeddingModel
from accesspilot.bootstrap import _configured_embedding, bootstrap_database
from accesspilot.config import Settings
from accesspilot.db.models import PolicyChunkRecord


def test_bootstrap_is_repeatable_and_indexes_every_policy(
    database_session_factory: sessionmaker[Session],
) -> None:
    first = bootstrap_database(
        database_session_factory,
        embedding_model=DeterministicEmbeddingModel(),
        embedding_mode="deterministic-offline",
    )
    second = bootstrap_database(
        database_session_factory,
        embedding_model=DeterministicEmbeddingModel(),
        embedding_mode="deterministic-offline",
    )

    with database_session_factory() as session:
        chunks = session.scalars(select(PolicyChunkRecord)).all()

    assert first == second
    assert first.policy_count == len(chunks)
    assert first.embedding_mode == "deterministic-offline"
    assert all(chunk.embedding is not None for chunk in chunks)
    assert all(len(chunk.embedding) == EMBEDDING_DIMENSIONS for chunk in chunks)

    # 不让本评测改变其他种子测试对“尚未建索引”初态的观察。
    with database_session_factory() as session:
        for chunk in session.scalars(select(PolicyChunkRecord)).all():
            chunk.embedding = None
        session.commit()


def test_offline_embedding_mode_overrides_configured_provider() -> None:
    model, mode = _configured_embedding(
        Settings(
            embedding_mode="deterministic-offline",
            dashscope_api_key="configured-but-unused",
            _env_file=None,
        )
    )

    assert isinstance(model, DeterministicEmbeddingModel)
    assert mode == "deterministic-offline"
