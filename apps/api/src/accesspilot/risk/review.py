"""严格只读的风险审查合同与执行用例。"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import EmbeddingModel
from accesspilot.db.models import AccessRequestRecord, EntitlementRecord
from accesspilot.rag.policies import PolicyMatch, search_policies


class MalformedRiskReviewError(ValueError):
    """模型输出无法按 RiskReview Schema 解析。"""


class RiskReviewFailed(RuntimeError):
    """风险审查遇到可恢复错误，调用方不得编造审查结论。"""


class RiskReviewRequestNotFoundError(LookupError):
    """风险审查目标申请不存在。"""


class PolicyCitation(BaseModel):
    """风险判断引用的一条已检索政策。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_code: str
    reason: str

    @field_validator("policy_code", "reason")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("政策引用字段不能为空")
        return normalized


class RiskReview(BaseModel):
    """只读风险 Agent 能返回的唯一结构化结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_level: Literal["low", "high", "critical"]
    outcome: Literal["clear", "requires_human_review", "blocked"]
    summary: str
    findings: list[str] = Field(min_length=1)
    citations: list[PolicyCitation] = Field(min_length=1, max_length=4)

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("风险摘要不能为空")
        return normalized

    @field_validator("findings")
    @classmethod
    def require_findings(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("风险发现不能为空")
        return normalized

    @field_validator("citations")
    @classmethod
    def require_unique_citations(
        cls,
        citations: list[PolicyCitation],
    ) -> list[PolicyCitation]:
        codes = [citation.policy_code for citation in citations]
        if len(codes) != len(set(codes)):
            raise ValueError("政策引用编号不能重复")
        return citations


class RiskReviewContext(BaseModel):
    """风险模型可读取的最小申请与政策快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    employee_id: str
    entitlement_id: str
    duration_days: int
    justification: str
    entitlement_risk_level: Literal["low", "high", "critical"]
    approval_policy: str
    policies: list[PolicyMatch] = Field(min_length=1, max_length=4)


class RiskReviewModel(Protocol):
    """根据只读上下文生成严格风险审查。"""

    def review(self, context: RiskReviewContext) -> RiskReview:
        """返回风险结论；不得修改任何业务状态。"""


def validate_risk_review_json(model_text: str) -> RiskReview:
    """把模型 JSON 校验为严格 RiskReview。"""

    try:
        return RiskReview.model_validate_json(model_text)
    except ValidationError as error:
        raise MalformedRiskReviewError("模型未返回合法的 RiskReview JSON") from error


def _build_query(
    request: AccessRequestRecord,
    entitlement: EntitlementRecord,
) -> str:
    return (
        f"申请人 {request.requester_id} 申请权限 {request.entitlement_code}；"
        f"权限风险等级 {entitlement.risk_level}，审批策略 {entitlement.approval_policy}；"
        f"申请 {request.duration_days} 天；理由：{request.justification}"
    )


def review_request_risk(
    session: Session,
    *,
    request_id: UUID,
    embedding_model: EmbeddingModel,
    review_model: RiskReviewModel,
) -> RiskReview:
    """只读申请和政策并返回审查；任何失败都不产生虚构业务事实。"""

    request = session.get(AccessRequestRecord, request_id)
    if request is None:
        raise RiskReviewRequestNotFoundError(str(request_id))
    entitlement = session.get(EntitlementRecord, request.entitlement_code)
    if entitlement is None:
        raise RiskReviewFailed("申请引用的权限目录不存在")

    try:
        policies = search_policies(
            session,
            _build_query(request, entitlement),
            embedding_model,
        )
        context = RiskReviewContext(
            request_id=request.id,
            employee_id=request.requester_id,
            entitlement_id=request.entitlement_code,
            duration_days=request.duration_days,
            justification=request.justification,
            entitlement_risk_level=entitlement.risk_level,
            approval_policy=entitlement.approval_policy,
            policies=policies,
        )
        review = review_model.review(context)
    except Exception as error:
        if isinstance(error, RiskReviewFailed):
            raise
        raise RiskReviewFailed("风险审查暂时不可用，请稍后重试") from error

    retrieved_codes = {policy.policy_code for policy in policies}
    cited_codes = {citation.policy_code for citation in review.citations}
    if not cited_codes <= retrieved_codes:
        raise RiskReviewFailed("风险模型引用了未检索到的政策")
    if review.risk_level != context.entitlement_risk_level:
        raise RiskReviewFailed("风险模型不得改变权限目录的风险等级")
    if context.entitlement_risk_level in {"high", "critical"} and (
        review.outcome == "clear"
    ):
        raise RiskReviewFailed("高风险权限不得被模型直接标记为无风险")
    return review


class DeterministicRiskReviewModel:
    """供离线演示与测试使用的透明规则审查器。"""

    def review(self, context: RiskReviewContext) -> RiskReview:
        if context.entitlement_risk_level == "critical":
            outcome: Literal["clear", "requires_human_review", "blocked"] = (
                "blocked"
            )
        elif context.entitlement_risk_level == "high":
            outcome = "requires_human_review"
        else:
            outcome = "clear"

        cited_policies = context.policies[:2]
        return RiskReview(
            risk_level=context.entitlement_risk_level,
            outcome=outcome,
            summary="离线规则审查已完成，最终权限仍取决于人工审批与开通状态。",
            findings=[
                f"申请期限为 {context.duration_days} 天。",
                f"目录审批策略为 {context.approval_policy}。",
            ],
            citations=[
                PolicyCitation(
                    policy_code=policy.policy_code,
                    reason="该政策与当前申请事实的离线向量相似度较高。",
                )
                for policy in cited_policies
            ],
        )
