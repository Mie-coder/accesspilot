"""Ground request extraction in this Workspace's eligible catalog and safe history."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.agent.structured_reply import StructuredReplyModel
from accesspilot.db.models import WorkspaceEventRecord, WorkspaceRecord
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.tools.catalog import list_eligible_access


def ground_request_model(
    model: StructuredReplyModel, session: Session, *, workspace_token: str,
    expected_field: str | None = None,
) -> StructuredReplyModel:
    """Keep the existing offline adapter; real extraction gets scoped context."""

    if not isinstance(model, DeepSeekStructuredReplyModel):
        return model
    return model.with_request_context(build_request_context(
        session, workspace_token=workspace_token, expected_field=expected_field,
    ))


def build_request_context(
    session: Session, *, workspace_token: str, expected_field: str | None = None,
) -> dict[str, object]:
    """Read only the bound Workspace's small, redacted interpretation context."""

    workspace = session.scalar(select(WorkspaceRecord).where(
        WorkspaceRecord.token_hash == hash_workspace_token(workspace_token),
    ))
    if workspace is None:
        raise ValueError("Workspace 不存在")
    catalog = list_eligible_access(session, workspace_token=workspace_token)
    events = session.scalars(select(WorkspaceEventRecord).where(
        WorkspaceEventRecord.workspace_id == workspace.id,
        WorkspaceEventRecord.event_type.in_((
            "message.user", "message.assistant", "message.completed",
        )),
    ).order_by(WorkspaceEventRecord.id.desc()).limit(6)).all()
    permissions = [
        {"code": permission.code, "name": permission.name,
         "system_code": permission.system_code, "system_name": permission.system_name}
        for permission in catalog.eligible_access or []
    ]
    history = [
        {"role": ("user" if event.event_type == "message.user" else "assistant"),
         "content": redact_sensitive_content(str(event.payload.get("content", "")))[:1000]}
        for event in reversed(events)
    ]
    return {
        "eligible_permissions": permissions,
        "expected_field": expected_field,
        "current_entitlement": (workspace.draft or {}).get("entitlement_id"),
        "recent_messages": history,
    }
