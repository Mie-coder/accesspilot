"""幂等权限开通、未知结果查询与安全恢复。"""

from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    ApprovalStepRecord,
    AuditEventRecord,
    ProvisioningAttemptRecord,
    utc_now,
)


class ProvisioningError(RuntimeError):
    """权限开通业务错误基类。"""


class ProvisioningNotFoundError(ProvisioningError):
    """Workspace 或申请不存在。"""


class ProvisioningWorkspaceMismatchError(ProvisioningError):
    """目标申请不属于当前 Workspace。"""


class ProvisioningForbiddenError(ProvisioningError):
    """当前 Principal 与 Case 有关系，但不承担开通职责。"""


class ApprovalRequiredError(ProvisioningError):
    """两级人工审批尚未全部通过。"""


class IdempotencyConflictError(ProvisioningError):
    """同一申请或幂等键与已有开通操作冲突。"""


class ProvisioningAttemptNotFoundError(ProvisioningError):
    """恢复前还没有任何开通尝试。"""


class IamOutcome(BaseModel):
    """IAM 模拟器返回的最小可信结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed", "unknown"]
    message: str | None = None


class IamProvisioner(Protocol):
    """外部 IAM 的创建与按幂等键查询边界。"""

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> IamOutcome:
        """尝试开通；超时必须返回 unknown，不能猜测失败或成功。"""

    def query_status(self, *, idempotency_key: str) -> IamOutcome:
        """查询同一外部操作的已知结果。"""


class SimulatedIamProvisioner:
    """支持成功、失败和响应丢失的本地 IAM 模拟器。"""

    def __init__(self) -> None:
        self._operations: dict[str, Literal["succeeded", "failed"]] = {}

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> IamOutcome:
        if fault_mode == "iam_failure":
            self._operations[idempotency_key] = "failed"
            return IamOutcome(status="failed", message="IAM 模拟失败")
        if fault_mode == "iam_timeout":
            # 模拟 IAM 已成功，但响应在返回 AccessPilot 前丢失。
            self._operations[idempotency_key] = "succeeded"
            return IamOutcome(status="unknown", message="IAM 响应超时，结果未知")
        self._operations[idempotency_key] = "succeeded"
        return IamOutcome(status="succeeded")

    def query_status(self, *, idempotency_key: str) -> IamOutcome:
        status = self._operations.get(idempotency_key)
        if status is None:
            return IamOutcome(status="unknown", message="IAM 中未找到该操作")
        return IamOutcome(status=status)


def _has_case_relation(
    session: Session,
    *,
    actor_id: str,
    roles: tuple[str, ...] | list[str] | set[str],
    request_id: UUID,
) -> bool:
    approver_roles = tuple(sorted(set(roles).intersection({"manager", "data_owner"})))
    approver_relation = exists(
        select(ApprovalStepRecord.id)
        .join(
            ApprovalCaseRecord,
            ApprovalCaseRecord.id == ApprovalStepRecord.approval_case_id,
        )
        .where(
            ApprovalCaseRecord.request_id == AccessRequestRecord.id,
            ApprovalStepRecord.approver_id == actor_id,
            ApprovalStepRecord.approver_role.in_(approver_roles),
            or_(
                ApprovalStepRecord.step_status == "pending",
                ApprovalStepRecord.decided_at.is_not(None),
            ),
        )
    ).correlate(AccessRequestRecord)
    return session.scalar(
        select(AccessRequestRecord.id).where(
            AccessRequestRecord.id == request_id,
            or_(AccessRequestRecord.requester_id == actor_id, approver_relation),
        )
    ) is not None


def _load_context(
    session: Session,
    *,
    actor_id: str,
    roles: tuple[str, ...] | list[str] | set[str],
    request_id: UUID,
) -> tuple[AccessRequestRecord, ApprovalCaseRecord]:
    """Authorize the Principal before exposing Case state, then lock its root."""

    is_permissions_admin = actor_id == "EMP-004" and "permissions_admin" in roles
    if is_permissions_admin:
        request = session.scalar(
            select(AccessRequestRecord)
            .where(
                AccessRequestRecord.id == request_id,
                exists(
                    select(ApprovalCaseRecord.id).where(
                        ApprovalCaseRecord.request_id == AccessRequestRecord.id,
                        ApprovalCaseRecord.workspace_id
                        == AccessRequestRecord.workspace_id,
                        ApprovalCaseRecord.approval_status == "approved",
                    )
                ).correlate(AccessRequestRecord),
            )
            .with_for_update()
        )
        if request is None:
            raise ProvisioningNotFoundError("申请不存在")
    else:
        if not _has_case_relation(
            session,
            actor_id=actor_id,
            roles=roles,
            request_id=request_id,
        ):
            raise ProvisioningNotFoundError("申请不存在")
        raise ProvisioningForbiddenError("当前账号没有权限开通职责")
    case = session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request_id,
            ApprovalCaseRecord.workspace_id == request.workspace_id,
        )
    )
    if case is None or case.approval_status != "approved":
        raise ApprovalRequiredError("人工审批尚未全部通过")
    return request, case


def _server_idempotency_key(request_id: UUID) -> str:
    return f"accesspilot:{request_id}"


def _apply_outcome(
    session: Session,
    *,
    request: AccessRequestRecord,
    attempt_id: UUID,
    outcome: IamOutcome,
) -> ProvisioningAttemptRecord:
    attempt = session.get(
        ProvisioningAttemptRecord,
        attempt_id,
        with_for_update=True,
    )
    if attempt is None:
        raise ProvisioningAttemptNotFoundError("开通尝试不存在")
    if attempt.provisioning_status == "succeeded":
        return attempt

    event_type = f"provisioning.{outcome.status}"
    attempt.provisioning_status = outcome.status
    attempt.last_error = outcome.message if outcome.status != "succeeded" else None
    attempt.updated_at = utc_now()

    try:
        if outcome.status == "succeeded":
            grant = session.scalar(
                select(AccessGrantRecord).where(
                    AccessGrantRecord.request_id == request.id
                )
            )
            if grant is None:
                starts_at = utc_now()
                session.add(
                    AccessGrantRecord(
                        workspace_id=request.workspace_id,
                        request_id=request.id,
                        idempotency_key=attempt.idempotency_key,
                        starts_at=starts_at,
                        expires_at=starts_at + timedelta(days=request.duration_days),
                    )
                )
        session.add(
            AuditEventRecord(
                workspace_id=request.workspace_id,
                request_id=request.id,
                actor_type="system",
                actor_id="iam-simulator",
                event_type=event_type,
                details={
                    "provisioning_attempt_id": str(attempt.id),
                    "idempotency_key": attempt.idempotency_key,
                    "attempt_count": attempt.attempt_count,
                    "message": outcome.message,
                },
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    session.refresh(attempt)
    return attempt


def provision_access(
    session: Session,
    *,
    request_id: UUID,
    actor_id: str,
    roles: tuple[str, ...] | list[str] | set[str],
    iam: IamProvisioner,
    fault_mode: str | None = None,
) -> ProvisioningAttemptRecord:
    """安全启动或重放同一 IAM 操作，成功时最多创建一条授权。"""

    request, _ = _load_context(
        session,
        actor_id=actor_id,
        roles=roles,
        request_id=request_id,
    )
    normalized_key = _server_idempotency_key(request.id)
    existing = session.scalar(
        select(ProvisioningAttemptRecord).where(
            ProvisioningAttemptRecord.request_id == request_id
        )
    )
    key_owner = session.scalar(
        select(ProvisioningAttemptRecord).where(
            ProvisioningAttemptRecord.idempotency_key == normalized_key
        )
    )
    if key_owner is not None and key_owner.request_id != request_id:
        raise IdempotencyConflictError("该幂等键已用于另一份申请")
    grant_key_owner = session.scalar(
        select(AccessGrantRecord).where(
            AccessGrantRecord.idempotency_key == normalized_key
        )
    )
    if grant_key_owner is not None and grant_key_owner.request_id != request_id:
        raise IdempotencyConflictError("该幂等键已用于另一份授权")
    if existing is not None and existing.idempotency_key != normalized_key:
        raise IdempotencyConflictError("该申请必须复用原幂等键")
    existing_grant = session.scalar(
        select(AccessGrantRecord).where(AccessGrantRecord.request_id == request_id)
    )
    if existing_grant is not None:
        if existing_grant.idempotency_key != normalized_key:
            raise IdempotencyConflictError("该申请已经使用另一幂等键完成授权")
        if existing is not None:
            if existing.provisioning_status != "succeeded":
                return _apply_outcome(
                    session,
                    request=request,
                    attempt_id=existing.id,
                    outcome=IamOutcome(status="succeeded"),
                )
            return existing
        try:
            reconciled = ProvisioningAttemptRecord(
                workspace_id=request.workspace_id,
                request_id=request.id,
                idempotency_key=normalized_key,
                provisioning_status="succeeded",
                attempt_count=1,
                last_error=None,
            )
            session.add(reconciled)
            session.flush()
            session.add(
                AuditEventRecord(
                    workspace_id=request.workspace_id,
                    request_id=request.id,
                    actor_type="system",
                    actor_id="accesspilot",
                    event_type="provisioning.reconciled",
                    details={
                        "provisioning_attempt_id": str(reconciled.id),
                        "idempotency_key": normalized_key,
                    },
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        session.refresh(reconciled)
        return reconciled
    if existing is not None and existing.provisioning_status in {
        "in_progress",
        "unknown",
        "succeeded",
    }:
        return existing

    try:
        if existing is None:
            attempt = ProvisioningAttemptRecord(
                workspace_id=request.workspace_id,
                request_id=request.id,
                idempotency_key=normalized_key,
                provisioning_status="in_progress",
                attempt_count=1,
                last_error=None,
            )
            session.add(attempt)
            event_type = "provisioning.started"
        else:
            attempt = existing
            attempt.provisioning_status = "in_progress"
            attempt.attempt_count += 1
            attempt.last_error = None
            attempt.updated_at = utc_now()
            event_type = "provisioning.retry_started"
        session.flush()
        session.add(
            AuditEventRecord(
                workspace_id=request.workspace_id,
                request_id=request.id,
                actor_type="employee",
                actor_id=actor_id,
                event_type=event_type,
                details={
                    "provisioning_attempt_id": str(attempt.id),
                    "idempotency_key": normalized_key,
                    "attempt_count": attempt.attempt_count,
                },
            )
        )
        # 先持久化 in_progress，进程在外部调用期间崩溃也能从数据库恢复。
        session.commit()
    except Exception:
        session.rollback()
        raise

    try:
        outcome = iam.provision(
            request_id=request.id,
            idempotency_key=normalized_key,
            fault_mode=fault_mode,
        )
    except Exception:
        outcome = IamOutcome(status="unknown", message="IAM 响应无法确认")
    return _apply_outcome(
        session,
        request=request,
        attempt_id=attempt.id,
        outcome=outcome,
    )


def recover_provisioning(
    session: Session,
    *,
    request_id: UUID,
    actor_id: str,
    roles: tuple[str, ...] | list[str] | set[str],
    iam: IamProvisioner,
) -> ProvisioningAttemptRecord:
    """按原幂等键查询未知操作；已成功时直接返回且不重复授权。"""

    request, _ = _load_context(
        session,
        actor_id=actor_id,
        roles=roles,
        request_id=request_id,
    )
    attempt = session.scalar(
        select(ProvisioningAttemptRecord)
        .where(ProvisioningAttemptRecord.request_id == request_id)
        .with_for_update()
    )
    if attempt is None:
        raise ProvisioningAttemptNotFoundError("还没有可恢复的开通尝试")
    if attempt.provisioning_status == "succeeded":
        return attempt

    # 保持 request/attempt 行锁直到外部查询和结果落库完成：并发恢复
    # 必须在首次恢复提交后重读状态，不能重复查询 IAM。
    attempt_id = attempt.id
    idempotency_key = attempt.idempotency_key
    try:
        outcome = iam.query_status(idempotency_key=idempotency_key)
    except Exception:
        outcome = IamOutcome(status="unknown", message="IAM 查询响应无法确认")
    return _apply_outcome(
        session,
        request=request,
        attempt_id=attempt_id,
        outcome=outcome,
    )
