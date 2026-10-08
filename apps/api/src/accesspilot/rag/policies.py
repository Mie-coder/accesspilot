"""政策向量写入与 pgvector 相似度检索。"""

import re
from collections.abc import Sequence
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import EMBEDDING_DIMENSIONS, EmbeddingModel
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.domain.catalog import POLICIES, POLICY_CODES


class PolicyEmbeddingError(RuntimeError):
    """政策向量无法完整、安全地写入。"""


class PolicyRetrievalError(RuntimeError):
    """政策向量无法用于可信检索。"""


class PolicyIndexUnavailableError(PolicyRetrievalError):
    """数据库中没有可检索的政策向量。"""


def _is_complete_policy_catalog(
    chunks: Sequence[PolicyChunkRecord],
) -> bool:
    return (
        tuple(chunk.policy_code for chunk in chunks) == POLICY_CODES
        and all(chunk.chunk_index == 0 for chunk in chunks)
    )


class PolicyMatch(BaseModel):
    """风险审查可以引用的一条只读政策检索结果。"""

    model_config = ConfigDict(extra="forbid")

    policy_code: str
    title: str
    content: str
    similarity: float
    version: str = "v1"
    source: str = "fictional_access_policy"
    # 问题与条款标题、常见问法共有的二字片段占比；只影响排序，不改变相似度。
    keyword_overlap: float = 0.0

    @property
    def rank_score(self) -> float:
        return self.similarity + KEYWORD_WEIGHT * self.keyword_overlap


# 向量相似度为主，关键词重合只用来拉开分数接近的条款。
KEYWORD_WEIGHT = 0.2
_SEARCH_HINTS = {policy.code: policy.search_hints for policy in POLICIES}


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[^\w]", "", text.casefold())
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


_KEYWORDS = {policy.code: _bigrams(policy.title + policy.search_hints) for policy in POLICIES}


def rank_policy_matches(query: str, matches: Sequence[PolicyMatch]) -> list[PolicyMatch]:
    """按“向量相似度 + 关键词重合”排序；同分时按政策编号保持稳定。"""

    query_grams = _bigrams(query)
    ranked = [
        match.model_copy(update={"keyword_overlap": len(
            query_grams & _KEYWORDS.get(match.policy_code, set()),
        ) / max(1, len(query_grams))})
        for match in matches
    ]
    return sorted(ranked, key=lambda match: (-match.rank_score, match.policy_code))


def _policy_text(chunk: PolicyChunkRecord) -> str:
    """被向量化的文本：条款原文加上只用于检索的常见问法。"""

    hints = _SEARCH_HINTS.get(chunk.policy_code, "")
    text = f"{chunk.title}\n{chunk.content}"
    return f"{text}\n常见问法：{hints}" if hints else text


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
    if not _is_complete_policy_catalog(chunks):
        raise PolicyEmbeddingError("政策事实源必须完整包含 POL-001 至 POL-008")
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
        stored_chunks = session.scalars(
            select(PolicyChunkRecord).order_by(
                PolicyChunkRecord.policy_code,
                PolicyChunkRecord.chunk_index,
            )
        ).all()
    except Exception as error:
        raise PolicyRetrievalError("政策索引状态读取失败") from error
    if not _is_complete_policy_catalog(stored_chunks) or any(
        chunk.embedding is None for chunk in stored_chunks
    ):
        raise PolicyIndexUnavailableError("政策向量索引不完整，可重试初始化")

    try:
        query_vectors = embedding_model.embed([query])
    except Exception as error:
        raise PolicyRetrievalError("政策问题向量化失败") from error
    if len(query_vectors) != 1 or len(query_vectors[0]) != EMBEDDING_DIMENSIONS:
        raise PolicyRetrievalError("政策问题向量必须固定为 512 维")

    # pgvector 的 SQLAlchemy 类型提供 cosine_distance；cast 只用于类型检查。
    embedding_column = cast(Any, PolicyChunkRecord.embedding)
    distance = embedding_column.cosine_distance(query_vectors[0]).label("distance")
    # 政策库只有八条，取回全部向量距离后再结合关键词重排，最后截取 limit 条。
    rows = session.execute(
        select(PolicyChunkRecord, distance)
        .where(PolicyChunkRecord.embedding.is_not(None))
        .order_by(distance, PolicyChunkRecord.policy_code)
    ).all()
    if not rows:
        raise PolicyIndexUnavailableError("政策向量尚未建立，可重试初始化")

    matches: list[PolicyMatch] = []
    for chunk, raw_distance in rows:
        similarity = max(0.0, min(1.0, 1.0 - float(raw_distance)))
        metadata = chunk.chunk_metadata
        version = metadata.get("version")
        source = metadata.get("source")
        matches.append(
            PolicyMatch(
                policy_code=chunk.policy_code,
                title=chunk.title,
                content=chunk.content,
                similarity=similarity,
                version=version if isinstance(version, str) and version else "v1",
                source=(
                    source
                    if isinstance(source, str) and source
                    else "fictional_access_policy"
                ),
            )
        )
    return rank_policy_matches(query, matches)[:limit]
