import accesspilot.domain.workflow as workflow


def test_request_status_values_are_stable() -> None:
    assert workflow.RequestStatus.DRAFT.value == "draft"
    assert workflow.RequestStatus.AWAITING_CONFIRMATION.value == "awaiting_confirmation"
    assert workflow.RequestStatus.SUBMITTED.value == "submitted"
    assert workflow.RequestStatus.CANCELLED.value == "cancelled"


def test_approval_status_values_are_stable() -> None:
    assert workflow.ApprovalStatus.NOT_STARTED.value == "not_started"
    assert workflow.ApprovalStatus.PENDING_MANAGER.value == "pending_manager"
    assert workflow.ApprovalStatus.PENDING_DATA_OWNER.value == "pending_data_owner"
    assert workflow.ApprovalStatus.APPROVED.value == "approved"
    assert workflow.ApprovalStatus.REJECTED.value == "rejected"


def test_provisioning_status_values_are_stable() -> None:
    assert workflow.ProvisioningStatus.NOT_STARTED.value == "not_started"
    assert workflow.ProvisioningStatus.IN_PROGRESS.value == "in_progress"
    assert workflow.ProvisioningStatus.SUCCEEDED.value == "succeeded"
    assert workflow.ProvisioningStatus.FAILED.value == "failed"
