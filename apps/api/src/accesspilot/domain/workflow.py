"""权限申请工作流状态与转换规则"""

from enum import StrEnum


# 申请本身的状态
class RequestStatus(StrEnum):
    DRAFT = "draft",
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"

#  审批状态
class ApprovalStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING_MANAGER = "pending_manager"
    PENDING_DATA_OWNER = "pending_data_owner"
    APPROVED = "approved"
    REJECTED = "rejected"

# 权限开通状态
class ProvisioningStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

def confirm_request(current_status: RequestStatus) -> RequestStatus:
    if current_status is not RequestStatus.AWAITING_CONFIRMATION:
        raise ValueError("只有等待确认的申请才能提交")

    return RequestStatus.SUBMITTED

def approve_by_manager(current_status: ApprovalStatus) -> ApprovalStatus:
    if current_status is not ApprovalStatus.PENDING_MANAGER:
        raise ValueError("当前不在经理审批阶段")

    return ApprovalStatus.PENDING_DATA_OWNER

def approve_by_data_owner(current_status: ApprovalStatus) -> ApprovalStatus:
    if current_status is not ApprovalStatus.PENDING_DATA_OWNER:
        raise ValueError("当前不在数据所有者审批阶段")

    return ApprovalStatus.APPROVED

def reject_approval(current_status: ApprovalStatus) -> ApprovalStatus:
    allowed_statuses = (
        ApprovalStatus.PENDING_MANAGER,
        ApprovalStatus.PENDING_DATA_OWNER,
    )

    if current_status not in allowed_statuses:
        raise ValueError("只有待审批的申请可以驳回")

    return ApprovalStatus.REJECTED

def mark_provisioning_failed(
    current_status: ProvisioningStatus,
) -> ProvisioningStatus:
    if current_status is not ProvisioningStatus.IN_PROGRESS:
        raise ValueError("只有开通中的任务可以标记为失败")

    return ProvisioningStatus.FAILED
