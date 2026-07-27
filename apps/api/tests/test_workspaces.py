from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import InMemoryWorkspaceStore, WorkspaceService


def test_reset_only_clears_the_target_workspace() -> None:
    service = WorkspaceService(InMemoryWorkspaceStore())
    first = service.create()
    second = service.create()
    first_draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="ENT-CUSTOMER-EXPORT",
    )
    second_draft = RequestDraft(
        employee_id="EMP-002",
        entitlement_id="ENT-OPS-LOG-READ",
    )
    service.save_draft(first.token, first_draft)
    service.save_draft(second.token, second_draft)

    service.reset(first.token)

    assert service.get(first.token).draft is None
    assert service.get(second.token).draft == second_draft
