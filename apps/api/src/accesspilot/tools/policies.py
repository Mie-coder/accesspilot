"""数据库事实驱动的政策目录、索引和三态问答服务。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import EmbeddingModel
from accesspilot.agent.routing import is_self_approval_question
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.domain.catalog import POLICY_CODES
from accesspilot.rag.policies import (
    PolicyMatch,
    index_policy_embeddings,
    search_policies,
)


class PolicyEvidence(BaseModel):
    """一条可展示、可审计且不暴露 ORM 的政策事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_code: str
    title: str
    content: str
    version: str
    source: str
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)


class PolicyAnswer(BaseModel):
    """政策问答的稳定业务状态，而不是供应商异常或自由文本状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "grounded",
        "insufficient_evidence",
        "retrieval_unavailable",
    ]
    answer: str
    evidence: list[PolicyEvidence] = Field(default_factory=list)
    next_step: str


class PolicyCatalogUnavailableError(RuntimeError):
    """数据库政策事实源不再满足固定的八条合同。"""


def _metadata_value(
    chunk: PolicyChunkRecord,
    key: str,
    default: str,
) -> str:
    value = chunk.chunk_metadata.get(key)
    return value if isinstance(value, str) and value else default


def _evidence_from_chunk(
    chunk: PolicyChunkRecord,
    *,
    similarity: float | None = None,
) -> PolicyEvidence:
    return PolicyEvidence(
        policy_code=chunk.policy_code,
        title=chunk.title,
        content=chunk.content,
        version=_metadata_value(chunk, "version", "v1"),
        source=_metadata_value(
            chunk,
            "source",
            "fictional_access_policy",
        ),
        similarity=similarity,
    )


def _evidence_from_match(match: PolicyMatch) -> PolicyEvidence:
    return PolicyEvidence(
        policy_code=match.policy_code,
        title=match.title,
        content=match.content,
        version=match.version,
        source=match.source,
        similarity=match.similarity,
    )


class PolicyService:
    """统一提供政策事实目录、完整索引和安全三态查询。"""

    def __init__(
        self,
        *,
        embedding_model: EmbeddingModel,
        similarity_threshold: float = 0.20,
    ) -> None:
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("政策相似度阈值必须在 0 到 1 之间")
        self._embedding_model = embedding_model
        self._similarity_threshold = similarity_threshold

    def catalog(self, session: Session) -> list[PolicyEvidence]:
        """从数据库事实源返回完整且稳定排序的政策目录。"""

        chunks = session.scalars(
            select(PolicyChunkRecord).order_by(
                PolicyChunkRecord.policy_code,
                PolicyChunkRecord.chunk_index,
            )
        ).all()
        if (
            tuple(chunk.policy_code for chunk in chunks) != POLICY_CODES
            or any(chunk.chunk_index != 0 for chunk in chunks)
        ):
            raise PolicyCatalogUnavailableError(
                "政策事实源必须完整包含 POL-001 至 POL-008"
            )
        return [_evidence_from_chunk(chunk) for chunk in chunks]

    def index(self, session: Session) -> int:
        """原子建立当前数据库中全部政策分块的向量索引。"""

        return index_policy_embeddings(session, self._embedding_model)

    def query(self, session: Session, query: str) -> PolicyAnswer:
        """回答政策问题；任何检索异常只映射为安全业务状态。"""

        if is_self_approval_question(query):
            return self._self_approval_answer(session)

        try:
            matches = search_policies(
                session,
                query,
                self._embedding_model,
                limit=4,
            )
        except Exception:
            return PolicyAnswer(
                status="retrieval_unavailable",
                answer="政策检索暂时不可用，当前无法提供可靠依据。",
                evidence=[],
                next_step="请稍后重试；如问题紧急，请联系人工安全流程。",
            )

        evidence = [
            _evidence_from_match(match)
            for match in matches
            if match.similarity >= self._similarity_threshold
        ]
        if not evidence:
            return PolicyAnswer(
                status="insufficient_evidence",
                answer="当前证据不足，无法可靠回答这个政策问题。",
                evidence=[],
                next_step="请补充具体权限、审批环节或业务场景后重试。",
            )

        answer = "；".join(
            f"根据 {item.policy_code}《{item.title}》：{item.content}"
            for item in evidence
        )
        return PolicyAnswer(
            status="grounded",
            answer=answer,
            evidence=evidence,
            next_step="请按上述政策核对申请字段和审批要求。",
        )

    def self_approval(self, session: Session) -> PolicyAnswer:
        """Read the fixed POL-006 fact without entering vector retrieval."""

        return self._self_approval_answer(session)

    def _self_approval_answer(self, session: Session) -> PolicyAnswer:
        try:
            chunk = session.scalar(
                select(PolicyChunkRecord)
                .where(PolicyChunkRecord.policy_code == "POL-006")
                .order_by(PolicyChunkRecord.chunk_index)
            )
        except Exception:
            chunk = None
        if chunk is None:
            return PolicyAnswer(
                status="retrieval_unavailable",
                answer="政策事实源暂时不可用，无法确认自审批规则。",
                evidence=[],
                next_step="请稍后重试或联系人工安全流程。",
            )

        evidence = _evidence_from_chunk(chunk)
        return PolicyAnswer(
            status="grounded",
            answer=(
                f"不能自审批。根据 {evidence.policy_code}《{evidence.title}》："
                f"{evidence.content}"
            ),
            evidence=[evidence],
            next_step="请由与申请人身份不同的直属经理或数据所有者审批。",
        )
