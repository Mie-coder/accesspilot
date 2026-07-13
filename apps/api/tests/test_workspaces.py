from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import InMemoryWorkspaceStore, WorkspaceService


def test_reset_only_clears_the_target_workspace() -> None:
    service = WorkspaceService(InMemoryWorkspaceStore())
    first = service.create()
    second = service.create()
    first_draft = RequestDraft(
        system_name="InsightHub",
        entitlement_name="客户数据导出",
    )
    second_draft = RequestDraft(
        system_name="OpsDesk",
        entitlement_name="运维日志查看",
    )
    service.save_draft(first.token, first_draft)
    service.save_draft(second.token, second_draft)

    service.reset(first.token)

    assert service.get(first.token).draft is None
    assert service.get(second.token).draft == second_draft
