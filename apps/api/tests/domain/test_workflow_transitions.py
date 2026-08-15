import pytest

import accesspilot.domain.workflow as workflow


def test_confirming_a_ready_request_submits_it() -> None:
    result = workflow.confirm_request(workflow.RequestStatus.AWAITING_CONFIRMATION)

    assert result is workflow.RequestStatus.SUBMITTED


def test_manager_approval_routes_to_data_owner() -> None:
    result = workflow.approve_by_manager(workflow.ApprovalStatus.PENDING_MANAGER)

    assert result is workflow.ApprovalStatus.PENDING_DATA_OWNER


def test_data_owner_approval_completes_approval() -> None:
    result = workflow.approve_by_data_owner(workflow.ApprovalStatus.PENDING_DATA_OWNER)

    assert result is workflow.ApprovalStatus.APPROVED


def test_rejection_ends_the_approval() -> None:
    result = workflow.reject_approval(workflow.ApprovalStatus.PENDING_MANAGER)

    assert result is workflow.ApprovalStatus.REJECTED


def test_provisioning_failure_is_recorded() -> None:
    result = workflow.mark_provisioning_failed(workflow.ProvisioningStatus.IN_PROGRESS)

    assert result is workflow.ProvisioningStatus.FAILED


def test_unconfirmed_request_cannot_be_submitted() -> None:
    with pytest.raises(ValueError):
        workflow.confirm_request(workflow.RequestStatus.DRAFT)


def test_manager_approval_cannot_start_before_approval_starts() -> None:
    with pytest.raises(ValueError):
        workflow.approve_by_manager(workflow.ApprovalStatus.NOT_STARTED)


def test_data_owner_cannot_be_approver_before_manager() -> None:
    with pytest.raises(ValueError):
        workflow.approve_by_data_owner(workflow.ApprovalStatus.PENDING_MANAGER)


def test_completed_approval_cannot_be_rejected() -> None:
    with pytest.raises(ValueError):
        workflow.reject_approval(workflow.ApprovalStatus.APPROVED)


def test_non_running_provisioning_cannot_be_marked_failed() -> None:
    with pytest.raises(ValueError):
        workflow.mark_provisioning_failed(workflow.ProvisioningStatus.NOT_STARTED)
