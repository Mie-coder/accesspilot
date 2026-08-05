"""正式权限申请的提交用例。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessRequestRecord,
    AuditEventRecord,
    WorkspaceRecord,
    utc_now,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.domain.models import RequestDraft
from accesspilot.events import stage_workspace_event
from accesspilot.tools.catalog import ToolResult, validate_access_request


class RequestNotReadyError(RuntimeError):
    """草稿尚未满足明确提交条件。"""


class RequestWorkspaceNotFoundError(RuntimeError):
    """提交时对应的 Workspace 已不存在。"""


class RequestActorMismatchError(RuntimeError):
    """草稿申请人与 Workspace 后端身份不一致。"""


class RequestValidationError(RuntimeError):
    """目录规则拒绝了这份申请。"""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.status)
        self.result = result


def submit_access_request(
    session: Session,
    *,
    workspace_token: str,
    draft: RequestDraft,
    actor_id: str | None = None,
) -> AccessRequestRecord:
    """确认并冻结草稿，同时追加一条申请提交审计事件。"""

    # 完整不等于已获授权：只有用户明确确认，才能跨过正式提交边界。
    if not draft.can_enter_approval():
        raise RequestNotReadyError

    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
        )
    )
    if workspace is None:
        raise RequestWorkspaceNotFoundError

    bound_actor_id = actor_id or workspace.actor_id
    if draft.employee_id != bound_actor_id or workspace.actor_id != bound_actor_id:
        raise RequestActorMismatchError

    validation = validate_access_request(session, draft)
    if validation.status != "success":
        raise RequestValidationError(validation)

    # can_enter_approval 已证明四个业务字段非空；显式断言也帮助类型检查器收窄类型。
    assert draft.employee_id is not None
    assert draft.entitlement_id is not None
    assert draft.duration_days is not None
    assert draft.justification is not None

    confirmed_at = utc_now()
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id=draft.employee_id,
        entitlement_code=draft.entitlement_id,
        duration_days=draft.duration_days,
        justification=draft.justification,
        request_status="submitted",
        confirmed_at=confirmed_at,
    )

    try:
        session.add(request)
        session.flush()
        session.add(
            AuditEventRecord(
                workspace_id=workspace.id,
                request_id=request.id,
                actor_type="employee",
                actor_id=draft.employee_id,
                event_type="request.submitted",
                details={
                    "employee_id": draft.employee_id,
                    "entitlement_id": draft.entitlement_id,
                    "duration_days": draft.duration_days,
                    "justification": draft.justification,
                },
            )
        )
        stage_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type="business.status",
            payload={
                "status": "submitted",
                "request_id": str(request.id),
            },
        )
        # 正式申请、审计与前端安全事件一起成功或回滚，避免出现不一致的业务事实。
        session.commit()
    except Exception:
        session.rollback()
        raise

    session.refresh(request)
    return request
