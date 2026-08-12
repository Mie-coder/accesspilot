"""Immutable Decision Packet generation and public projection."""

from __future__ import annotations

import re
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import EmbeddingModel
from accesspilot.approvals import ApprovalRoutingError, build_approval_route
from accesspilot.db.models import (
    AccessRequestRecord,
    AuditEventRecord,
    DecisionPacketRecord,
    EmployeeRecord,
    EntitlementRecord,
)
from accesspilot.rag.policies import PolicyMatch, search_policies

PACKET_VERSION = "v1"
CATALOG_VERSION = "fictional-catalog-v1"
GenerationMode = Literal["provider", "deterministic", "unavailable"]
SourceKind = Literal["verified_fact", "policy_evidence", "user_claim", "advisory"]


class DecisionPacketError(RuntimeError):
    """Decision Packet business error base."""


class DecisionPacketNotFoundError(DecisionPacketError):
    """The request does not exist or is unrelated to the Principal."""


class DecisionPacketStateError(DecisionPacketError):
    """Only a submitted formal request may create its Packet."""


class DecisionPacketGenerationError(DecisionPacketError):
    """Local catalog or policy evidence could not be frozen."""


class MalformedDecisionAdvisoryError(ValueError):
    """A provider returned content outside the strict advisory schema."""


class DecisionAdvisory(BaseModel):
    """The only model-controlled, non-authoritative Packet section."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment: Literal["clear", "risk", "blocked"]
    summary: str = Field(min_length=1, max_length=600)
    unknowns: list[str] = Field(max_length=5)
    recommendations: list[str] = Field(max_length=5)
    citations: list[str] = Field(max_length=4)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("风险建议摘要不能为空")
        return normalized

    @field_validator("unknowns", "recommendations", "citations")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("风险建议列表不能包含空值")
        if len(normalized) != len(set(normalized)):
            raise ValueError("风险建议列表不能重复")
        return normalized


class AdvisoryPolicyEvidence(BaseModel):
    """Current policy evidence sent to the bounded advisory adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_code: str
    title: str
    content: str
    version: str
    source: str


class DecisionAdvisoryContext(BaseModel):
    """De-identified facts allowed to leave the Decision Packet service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_days: int = Field(gt=0)
    justification: str
    risk_level: Literal["low", "high", "critical"]
    max_duration_days: int | None
    approval_policy: str
    policies: list[AdvisoryPolicyEvidence] = Field(min_length=1, max_length=4)


class DecisionAdvisoryModel(Protocol):
    """Optional external/deterministic advisory boundary."""

    generation_mode: Literal["provider", "deterministic"]

    def review(self, context: DecisionAdvisoryContext) -> DecisionAdvisory:
        """Return advisory text only; routes and decisions are out of schema."""


class FrozenRequestFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requester_id: str
    requester_name: str
    entitlement_code: str
    entitlement_name: str
    duration_days: int
    justification: str
    request_status: str
    confirmed_at: str


class FrozenCatalogFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_level: str
    approval_policy: str
    max_duration_days: int | None


class FixedRouteStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_order: int
    approver_id: str
    approver_role: str


class DecisionPacketItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: SourceKind
    source_ref: str
    source_version: str
    title: str
    content: str
    generation_mode: GenerationMode | None = None


class DecisionPacketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    frozen_request: FrozenRequestFacts
    catalog: FrozenCatalogFacts
    fixed_route: list[FixedRouteStep]
    items: list[DecisionPacketItem]
    advisory: DecisionAdvisory | None
    availability_message: str | None


class DecisionPacketPayload(DecisionPacketSnapshot):
    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: str
    request_id: str
    packet_version: str
    generation_mode: GenerationMode
    catalog_version: str
    created_at: str


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_EMPLOYEE = re.compile(r"\bEMP-\d+\b", re.IGNORECASE)
_SECRET = re.compile(r"\b(?:sk|key|token|secret)[-_][A-Z0-9_-]+\b", re.IGNORECASE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_FORBIDDEN_OUTPUT = re.compile(
    r"EMP-\d+|approver_(?:id|role)|approval_route|idempotency|"
    r"\b(?:approve|reject|provision)\b|https?://|\bsk-[A-Z0-9_-]+",
    re.IGNORECASE,
)


def _redact_for_advisory(value: str) -> str:
    redacted = _EMAIL.sub("[已脱敏联系方式]", value)
    redacted = _EMPLOYEE.sub("[已脱敏员工]", redacted)
    redacted = _SECRET.sub("[已脱敏密钥]", redacted)
    return _URL.sub("[已脱敏地址]", redacted)


def _advisory_is_safe(
    advisory: DecisionAdvisory,
    *,
    entitlement_code: str,
) -> bool:
    text = "\n".join(
        [
            advisory.summary,
            *advisory.unknowns,
            *advisory.recommendations,
        ]
    )
    return entitlement_code.casefold() not in text.casefold() and not _FORBIDDEN_OUTPUT.search(
        text
    )


def _advisory_context(
    request: AccessRequestRecord,
    entitlement: EntitlementRecord,
    policies: list[PolicyMatch],
) -> DecisionAdvisoryContext:
    return DecisionAdvisoryContext(
        duration_days=request.duration_days,
        justification=_redact_for_advisory(request.justification),
        risk_level=entitlement.risk_level,
        max_duration_days=entitlement.max_duration_days,
        approval_policy=entitlement.approval_policy,
        policies=[
            AdvisoryPolicyEvidence(
                policy_code=policy.policy_code,
                title=policy.title,
                content=policy.content,
                version=policy.version,
                source=policy.source,
            )
            for policy in policies
        ],
    )


def _review_advisory(
    *,
    request: AccessRequestRecord,
    entitlement: EntitlementRecord,
    policies: list[PolicyMatch],
    advisory_model: DecisionAdvisoryModel | None,
) -> tuple[GenerationMode, DecisionAdvisory | None, str | None]:
    unavailable = "AI 风险建议不可用；已验证事实、政策证据和固定人工审批路线不受影响。"
    if advisory_model is None:
        return "unavailable", None, unavailable
    try:
        advisory = advisory_model.review(
            _advisory_context(request, entitlement, policies)
        )
        known_codes = {policy.policy_code for policy in policies}
        if not set(advisory.citations) <= known_codes:
            return "unavailable", None, unavailable
        if not _advisory_is_safe(
            advisory,
            entitlement_code=request.entitlement_code,
        ):
            return "unavailable", None, unavailable
        return advisory_model.generation_mode, advisory, None
    except Exception:
        return "unavailable", None, unavailable


def _packet_snapshot(
    *,
    request: AccessRequestRecord,
    requester: EmployeeRecord,
    entitlement: EntitlementRecord,
    policies: list[PolicyMatch],
    route: list[tuple[str, str]],
    advisory_model: DecisionAdvisoryModel | None,
) -> tuple[GenerationMode, DecisionPacketSnapshot]:
    generation_mode, advisory, availability_message = _review_advisory(
        request=request,
        entitlement=entitlement,
        policies=policies,
        advisory_model=advisory_model,
    )
    items = [
        DecisionPacketItem(
            source_kind="verified_fact",
            source_ref=f"request:{request.id}",
            source_version=PACKET_VERSION,
            title="已验证申请事实",
            content=(
                f"{requester.name} 申请 {entitlement.name}，"
                f"期限 {request.duration_days} 天。"
            ),
        ),
        DecisionPacketItem(
            source_kind="verified_fact",
            source_ref=f"catalog:{entitlement.code}",
            source_version=CATALOG_VERSION,
            title="已验证目录事实",
            content=(
                f"风险等级 {entitlement.risk_level}，"
                f"审批策略 {entitlement.approval_policy}，"
                f"最长期限 {entitlement.max_duration_days or '未设置'} 天。"
            ),
        ),
        DecisionPacketItem(
            source_kind="user_claim",
            source_ref=f"request:{request.id}:justification",
            source_version=PACKET_VERSION,
            title="申请人说明",
            content=request.justification,
        ),
        *[
            DecisionPacketItem(
                source_kind="policy_evidence",
                source_ref=f"policy:{policy.policy_code}",
                source_version=policy.version,
                title=policy.title,
                content=policy.content,
            )
            for policy in policies
        ],
        DecisionPacketItem(
            source_kind="advisory",
            source_ref="advisory:decision-packet",
            source_version=PACKET_VERSION,
            title=(
                "AI 建议"
                if generation_mode == "provider"
                else "规则建议"
                if generation_mode == "deterministic"
                else "建议不可用"
            ),
            content=(
                advisory.summary
                if advisory is not None
                else availability_message or "建议不可用。"
            ),
            generation_mode=generation_mode,
        ),
    ]
    snapshot = DecisionPacketSnapshot(
        frozen_request=FrozenRequestFacts(
            requester_id=request.requester_id,
            requester_name=requester.name,
            entitlement_code=request.entitlement_code,
            entitlement_name=entitlement.name,
            duration_days=request.duration_days,
            justification=request.justification,
            request_status=request.request_status,
            confirmed_at=request.confirmed_at.isoformat(),
        ),
        catalog=FrozenCatalogFacts(
            risk_level=entitlement.risk_level,
            approval_policy=entitlement.approval_policy,
            max_duration_days=entitlement.max_duration_days,
        ),
        fixed_route=[
            FixedRouteStep(
                step_order=index,
                approver_id=approver_id,
                approver_role=role,
            )
            for index, (approver_id, role) in enumerate(route, start=1)
        ],
        items=items,
        advisory=advisory,
        availability_message=availability_message,
    )
    return generation_mode, snapshot


def generate_decision_packet(
    session: Session,
    *,
    request_id: UUID,
    actor_id: str,
    embedding_model: EmbeddingModel,
    advisory_model: DecisionAdvisoryModel | None,
) -> tuple[DecisionPacketRecord, bool]:
    """Create or return the immutable Packet after requester ACL and row lock."""

    request = session.scalar(
        select(AccessRequestRecord)
        .where(
            AccessRequestRecord.id == request_id,
            AccessRequestRecord.requester_id == actor_id,
        )
        .with_for_update()
    )
    if request is None:
        raise DecisionPacketNotFoundError("申请不存在")
    existing = session.scalar(
        select(DecisionPacketRecord).where(
            DecisionPacketRecord.request_id == request.id
        )
    )
    if existing is not None:
        return existing, False
    if request.request_status != "submitted":
        raise DecisionPacketStateError("只有已提交申请才能生成决策材料")

    requester = session.get(EmployeeRecord, request.requester_id)
    entitlement = session.get(EntitlementRecord, request.entitlement_code)
    if requester is None or entitlement is None:
        raise DecisionPacketGenerationError("申请引用的目录事实不完整")
    try:
        route = build_approval_route(requester, entitlement)
        policies = search_policies(
            session,
            (
                f"权限风险 {entitlement.risk_level}；"
                f"审批策略 {entitlement.approval_policy}；"
                f"期限 {request.duration_days} 天；"
                f"理由 {_redact_for_advisory(request.justification)}"
            ),
            embedding_model,
        )
    except ApprovalRoutingError:
        raise
    except Exception as error:
        raise DecisionPacketGenerationError("政策证据暂时无法冻结，请重试") from error

    generation_mode, snapshot = _packet_snapshot(
        request=request,
        requester=requester,
        entitlement=entitlement,
        policies=policies,
        route=route,
        advisory_model=advisory_model,
    )
    packet = DecisionPacketRecord(
        request_id=request.id,
        packet_version=PACKET_VERSION,
        generation_mode=generation_mode,
        catalog_version=CATALOG_VERSION,
        frozen_content=snapshot.model_dump(mode="json"),
    )
    try:
        session.add(packet)
        session.flush()
        session.add(
            AuditEventRecord(
                workspace_id=request.workspace_id,
                request_id=request.id,
                actor_type="employee",
                actor_id=actor_id,
                event_type="decision_packet.created",
                details={
                    "status": generation_mode,
                    "next_step": "核对决策材料并创建固定审批路线",
                },
            )
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise DecisionPacketGenerationError(
            "决策材料与审计事实暂时无法原子保存，请重试"
        ) from error
    session.refresh(packet)
    return packet, True


def decision_packet_payload(packet: DecisionPacketRecord) -> dict[str, object]:
    """Validate stored JSON before returning the strictly public Packet DTO."""

    snapshot = DecisionPacketSnapshot.model_validate(packet.frozen_content)
    payload = DecisionPacketPayload(
        **snapshot.model_dump(),
        packet_id=str(packet.id),
        request_id=str(packet.request_id),
        packet_version=packet.packet_version,
        generation_mode=packet.generation_mode,
        catalog_version=packet.catalog_version,
        created_at=packet.created_at.isoformat(),
    )
    return payload.model_dump(mode="json")
