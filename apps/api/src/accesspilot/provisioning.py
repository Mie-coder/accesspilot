"""幂等权限开通、未知结果查询与安全恢复。"""

from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    AuditEventRecord,
    ProvisioningAttemptRecord,
    WorkspaceRecord,
    utc_now,
)
from accesspilot.db.workspace_store import hash_workspace_token

_UNSET_FAULT_MODE = object()



class ProvisioningError(RuntimeError):
    """权限开通业务错误基类。"""


class ProvisioningNotFoundError(ProvisioningError):
    """Workspace 或申请不存在。"""


class ProvisioningWorkspaceMismatchError(ProvisioningError):
    """目标申请不属于当前 Workspace。"""


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


def _load_context(
    session: Session,
    *,
    workspace_token: str,
    request_id: UUID,
) -> tuple[WorkspaceRecord, AccessRequestRecord, ApprovalCaseRecord]:
    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
        )
    )
    if workspace is None:
        raise ProvisioningNotFoundError("Workspace 不存在")
    request = session.get(AccessRequestRecord, request_id)
    if request is None:
        raise ProvisioningNotFoundError("申请不存在")
    if request.workspace_id != workspace.id:
        raise ProvisioningWorkspaceMismatchError("申请不属于当前 Workspace")
    case = session.scalar(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.request_id == request_id
        )
    )
    if case is None or case.approval_status != "approved":
        raise ApprovalRequiredError("人工审批尚未全部通过")
    return workspace, request, case


def _validate_idempotency_key(idempotency_key: str) -> str:
    normalized = idempotency_key.strip()
    if not normalized or len(normalized) > 100:
        raise IdempotencyConflictError("幂等键长度必须在 1 到 100 之间")
    return normalized


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
    workspace_token: str,
    request_id: UUID,
    idempotency_key: str,
    iam: IamProvisioner,
    fault_mode: str | None | object = _UNSET_FAULT_MODE,
) -> ProvisioningAttemptRecord:
    """安全启动或重放同一 IAM 操作，成功时最多创建一条授权。"""

    normalized_key = _validate_idempotency_key(idempotency_key)
    workspace, request, _ = _load_context(
        session,
        workspace_token=workspace_token,
        request_id=request_id,
    )
    existing = session.scalar(
        select(ProvisioningAttemptRecord)
        .where(ProvisioningAttemptRecord.request_id == request_id)
        .with_for_update()
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
                workspace_id=workspace.id,
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
                    workspace_id=workspace.id,
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
                workspace_id=workspace.id,
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
                workspace_id=workspace.id,
                request_id=request.id,
                actor_type="system",
                actor_id="accesspilot",
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
        active_fault_mode = workspace.fault_mode if fault_mode is _UNSET_FAULT_MODE else fault_mode
        outcome = iam.provision(
            request_id=request.id,
            idempotency_key=normalized_key,
            fault_mode=active_fault_mode if isinstance(active_fault_mode, str) else None,
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
    workspace_token: str,
    request_id: UUID,
    iam: IamProvisioner,
) -> ProvisioningAttemptRecord:
    """按原幂等键查询未知操作；已成功时直接返回且不重复授权。"""

    _, request, _ = _load_context(
        session,
        workspace_token=workspace_token,
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

    # 查询外部状态前释放数据库行锁，避免网络等待阻塞其他只读请求。
    attempt_id = attempt.id
    idempotency_key = attempt.idempotency_key
    session.commit()
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
