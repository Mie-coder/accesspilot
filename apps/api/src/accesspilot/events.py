"""安全 Workspace 事件、SSE 回放与模型调用配额。"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import WorkspaceEventRecord, WorkspaceRecord
from accesspilot.db.workspace_store import hash_workspace_token


class UnsafeEventError(ValueError):
    """事件类型或 payload 不允许发送给前端。"""


class EventWorkspaceNotFoundError(LookupError):
    """事件目标 Workspace 不存在。"""


class ModelQuotaExceededError(RuntimeError):
    """当前 Workspace 已耗尽模型调用额度。"""


class MessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=10_000)


class ToolSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=1_000)


class DraftUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: dict[str, Any]
    missing_fields: list[str]
    can_enter_approval: bool


class BusinessStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=80)
    request_id: str | None = None


class RecoverableErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)


SAFE_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "message.user": MessagePayload,
    "message.assistant": MessagePayload,
    "tool.summary": ToolSummaryPayload,
    "draft.updated": DraftUpdatedPayload,
    "business.status": BusinessStatusPayload,
    "error.recoverable": RecoverableErrorPayload,
}

FORBIDDEN_EVENT_KEYS = {
    "api_key",
    "authorization",
    "chain_of_thought",
    "hidden_reasoning",
    "hidden_thoughts",
    "reasoning_content",
    "secret",
}


class ModelQuota(BaseModel):
    """Workspace 当前可向前端展示的配额快照。"""

    model_config = ConfigDict(frozen=True)

    used: int
    limit: int
    remaining: int


def _load_workspace(session: Session, token: str) -> WorkspaceRecord:
    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == hash_workspace_token(token)
        )
    )
    if workspace is None:
        raise EventWorkspaceNotFoundError(token)
    return workspace


def _has_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in FORBIDDEN_EVENT_KEYS or _has_forbidden_key(nested):
                return True
    elif isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


def _validate_event_payload(
    event_type: str,
    payload: dict[str, object],
) -> dict[str, Any]:
    model = SAFE_EVENT_MODELS.get(event_type)
    if model is None or _has_forbidden_key(payload):
        raise UnsafeEventError("事件类型或字段不在前端安全白名单中")
    try:
        validated = model.model_validate(payload)
    except ValidationError as error:
        raise UnsafeEventError("事件 payload 不符合安全 Schema") from error
    return validated.model_dump(mode="json", exclude_none=True)


def append_workspace_event(
    session: Session,
    *,
    workspace_token: str,
    event_type: str,
    payload: dict[str, object],
) -> WorkspaceEventRecord:
    """校验并持久化一条可发送给浏览器的事件。"""

    try:
        event = stage_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type=event_type,
            payload=payload,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(event)
    return event


def stage_workspace_event(
    session: Session,
    *,
    workspace_token: str,
    event_type: str,
    payload: dict[str, object],
) -> WorkspaceEventRecord:
    """校验并暂存事件，由调用方与其他业务事实一起提交。"""

    safe_payload = _validate_event_payload(event_type, payload)
    workspace = _load_workspace(session, workspace_token)
    event = WorkspaceEventRecord(
        workspace_id=workspace.id,
        event_type=event_type,
        payload=safe_payload,
    )
    session.add(event)
    return event


def list_workspace_events(
    session: Session,
    *,
    workspace_token: str,
    after_id: int = 0,
) -> list[WorkspaceEventRecord]:
    """只读返回当前 Workspace 在游标之后的安全事件。"""

    if after_id < 0:
        raise ValueError("事件游标不能为负数")
    workspace = _load_workspace(session, workspace_token)
    return list(
        session.scalars(
            select(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.id > after_id,
            )
            .order_by(WorkspaceEventRecord.id)
        ).all()
    )


def format_sse_event(event: WorkspaceEventRecord) -> str:
    """把已验证的数据库事件编码成标准 SSE 文本块。"""

    data = json.dumps(
        event.payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"


def _quota(workspace: WorkspaceRecord) -> ModelQuota:
    return ModelQuota(
        used=workspace.model_calls_used,
        limit=workspace.model_call_limit,
        remaining=workspace.model_call_limit - workspace.model_calls_used,
    )


def get_model_quota(
    session: Session,
    *,
    workspace_token: str,
) -> ModelQuota:
    """只读配额快照，不消耗任何额度。"""

    return _quota(_load_workspace(session, workspace_token))


def consume_model_call(
    session: Session,
    *,
    workspace_token: str,
) -> ModelQuota:
    """用 Workspace 行锁原子消费一次模型调用额度。"""

    workspace = session.scalar(
        select(WorkspaceRecord)
        .where(WorkspaceRecord.token_hash == hash_workspace_token(workspace_token))
        .with_for_update()
    )
    if workspace is None:
        raise EventWorkspaceNotFoundError(workspace_token)
    if workspace.model_calls_used >= workspace.model_call_limit:
        session.rollback()
        raise ModelQuotaExceededError("模型调用额度已用尽")
    workspace.model_calls_used += 1
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(workspace)
    return _quota(workspace)
