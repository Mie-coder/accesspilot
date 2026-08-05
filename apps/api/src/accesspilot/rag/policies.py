"""政策向量写入与 pgvector 相似度检索。"""

from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import EMBEDDING_DIMENSIONS, EmbeddingModel
from accesspilot.db.models import PolicyChunkRecord


class PolicyEmbeddingError(RuntimeError):
    """政策向量无法完整、安全地写入。"""


class PolicyRetrievalError(RuntimeError):
    """政策向量无法用于可信检索。"""


class PolicyIndexUnavailableError(PolicyRetrievalError):
    """数据库中没有可检索的政策向量。"""


class PolicyMatch(BaseModel):
    """风险审查可以引用的一条只读政策检索结果。"""

    model_config = ConfigDict(extra="forbid")

    policy_code: str
    title: str
    content: str
    similarity: float


def _policy_text(chunk: PolicyChunkRecord) -> str:
    return f"{chunk.title}\n{chunk.content}"


def _require_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
) -> None:
    if len(vectors) != expected_count:
        raise PolicyEmbeddingError("向量数量与政策分块数量不一致")
    if any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
        raise PolicyEmbeddingError("政策向量必须固定为 512 维")


def index_policy_embeddings(
    session: Session,
    embedding_model: EmbeddingModel,
) -> int:
    """原子更新所有原创政策向量，并返回写入数量。"""

    chunks = session.scalars(
        select(PolicyChunkRecord).order_by(
            PolicyChunkRecord.policy_code,
            PolicyChunkRecord.chunk_index,
        )
    ).all()
    texts = [_policy_text(chunk) for chunk in chunks]

    try:
        vectors = embedding_model.embed(texts)
        _require_vectors(vectors, expected_count=len(chunks))
        # 完整批次先通过结构校验，再触碰 ORM 字段，防止只写入一部分。
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
        session.commit()
    except Exception as error:
        session.rollback()
        if isinstance(error, PolicyEmbeddingError):
            raise
        raise PolicyEmbeddingError("政策向量写入失败") from error

    return len(chunks)


def search_policies(
    session: Session,
    query: str,
    embedding_model: EmbeddingModel,
    *,
    limit: int = 4,
) -> list[PolicyMatch]:
    """使用 pgvector 余弦距离检索最相关的只读政策引用。"""

    if not query.strip():
        raise PolicyRetrievalError("政策检索问题不能为空")
    if limit <= 0:
        raise PolicyRetrievalError("政策检索数量必须为正数")

    try:
        query_vectors = embedding_model.embed([query])
    except Exception as error:
        raise PolicyRetrievalError("政策问题向量化失败") from error
    if len(query_vectors) != 1 or len(query_vectors[0]) != EMBEDDING_DIMENSIONS:
        raise PolicyRetrievalError("政策问题向量必须固定为 512 维")

    # pgvector 的 SQLAlchemy 类型提供 cosine_distance；cast 只用于类型检查。
    embedding_column = cast(Any, PolicyChunkRecord.embedding)
    distance = embedding_column.cosine_distance(query_vectors[0]).label("distance")
    rows = session.execute(
        select(PolicyChunkRecord, distance)
        .where(PolicyChunkRecord.embedding.is_not(None))
        .order_by(distance, PolicyChunkRecord.policy_code)
        .limit(limit)
    ).all()
    if not rows:
        raise PolicyIndexUnavailableError("政策向量尚未建立，可重试初始化")

    matches: list[PolicyMatch] = []
    for chunk, raw_distance in rows:
        similarity = max(0.0, min(1.0, 1.0 - float(raw_distance)))
        matches.append(
            PolicyMatch(
                policy_code=chunk.policy_code,
                title=chunk.title,
                content=chunk.content,
                similarity=similarity,
            )
        )
    return matches
