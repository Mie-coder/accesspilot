"""安全 Workspace 事件、SSE 回放与模型调用配额。"""

import json
import re
from datetime import UTC, datetime, timedelta
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


class TurnInProgressError(RuntimeError):
    """当前 Workspace 已有未完成对话轮。"""


class ModelQuotaExceededError(RuntimeError):
    """当前 Workspace 已耗尽模型调用额度。"""


class MessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=10_000)
    turn_id: str | None = Field(default=None, min_length=1, max_length=120)


class TurnStartedPayload(BaseModel):
    """当前对话轮开始事实。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=120)
    lease_expires_at: datetime | None = None


class IntentDetectedPayload(BaseModel):
    """路由器确定的单一业务意图。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=120)
    intent: str = Field(min_length=1, max_length=80)
    security_flagged: bool = False


class ToolSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=1_000)
    turn_id: str | None = Field(default=None, min_length=1, max_length=120)


class ToolStartedPayload(BaseModel):
    """只读工具开始事实。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=120)
    tool: str = Field(min_length=1, max_length=100)
    tool_call_id: str = Field(min_length=1, max_length=120)


class ToolCompletedPayload(ToolSummaryPayload):
    """只读工具完成事实。"""

    tool_call_id: str = Field(min_length=1, max_length=120)


class DraftUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: dict[str, Any]
    missing_fields: list[str]
    can_enter_approval: bool
    draft_revision: int = 0
    turn_id: str | None = Field(default=None, min_length=1, max_length=120)


class BusinessStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=80)
    request_id: str | None = None
    turn_id: str | None = Field(default=None, min_length=1, max_length=120)


class RecoverableErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    intent: str | None = Field(default=None, min_length=1, max_length=80)
    business_status: str | None = Field(default=None, min_length=1, max_length=80)
    draft_revision: int | None = Field(default=None, ge=0)
    draft: dict[str, Any] | None = None
    assistant_message: str | None = Field(default=None, min_length=1, max_length=10_000)
    error_code: str | None = Field(default=None, min_length=1, max_length=120)
    turn_id: str | None = Field(default=None, min_length=1, max_length=120)


class SecurityNoticePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    turn_id: str | None = Field(default=None, min_length=1, max_length=120)


class MessageCompletedPayload(BaseModel):
    """唯一成功终态；持久化 payload 不包含自身数据库 ID。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=120)
    message_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=10_000)
    intent: str | None = Field(default=None, min_length=1, max_length=80)
    business_status: str | None = Field(default=None, min_length=1, max_length=80)
    draft_revision: int | None = Field(default=None, ge=0)
    draft: dict[str, Any] | None = None
    assistant_message: str | None = Field(default=None, min_length=1, max_length=10_000)
    error_code: str | None = Field(default=None, min_length=1, max_length=120)


class TurnInterruptedPayload(BaseModel):
    """客户端取消或连接断开后的唯一中断终态。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=120)
    retryable: bool = True


SAFE_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "turn.started": TurnStartedPayload,
    "intent.detected": IntentDetectedPayload,
    "message.user": MessagePayload,
    "message.assistant": MessagePayload,
    "tool.summary": ToolSummaryPayload,
    "tool.started": ToolStartedPayload,
    "tool.completed": ToolCompletedPayload,
    "draft.updated": DraftUpdatedPayload,
    "business.status": BusinessStatusPayload,
    "error.recoverable": RecoverableErrorPayload,
    "security.notice": SecurityNoticePayload,
    "message.completed": MessageCompletedPayload,
    "turn.interrupted": TurnInterruptedPayload,
}

TERMINAL_EVENT_TYPES = frozenset(
    {"message.completed", "error.recoverable", "turn.interrupted"}
)

FORBIDDEN_EVENT_KEYS = {
    "api_key",
    "authorization",
    "chain_of_thought",
    "hidden_reasoning",
    "hidden_thoughts",
    "reasoning_content",
    "secret",
    "quota",
    "used",
    "limit",
    "remaining",
    "retry_consumed",
    "model_calls_used",
    "model_call_limit",
    "model_quota",
}

SENSITIVE_EVENT_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|ds)-[a-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(
        r"(?i)(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|"
        r"refresh[_ -]?token|private[_ -]?key|password)\s*[:=]\s*\S+"
    ),
)


class ModelQuota(BaseModel):
    """Workspace 当前可向前端展示的配额快照。"""

    model_config = ConfigDict(frozen=True)

    used: int
    limit: int
    remaining: int
    retry_consumed: int


def _load_workspace(session: Session, token: str) -> WorkspaceRecord:
    workspace = session.scalar(
        select(WorkspaceRecord).where(WorkspaceRecord.token_hash == hash_workspace_token(token))
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


def _has_sensitive_value(value: object) -> bool:
    """递归拦截 payload 字符串里的常见凭证形态。"""

    if isinstance(value, dict):
        return any(_has_sensitive_value(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_has_sensitive_value(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) is not None for pattern in SENSITIVE_EVENT_VALUE_PATTERNS)
    return False


def validate_event_payload(
    event_type: str,
    payload: dict[str, object],
) -> dict[str, Any]:
    """在写入数据库或其他业务状态前校验事件安全边界。"""

    model = SAFE_EVENT_MODELS.get(event_type)
    if model is None or _has_forbidden_key(payload) or _has_sensitive_value(payload):
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
    event_key: str | None = None,
) -> WorkspaceEventRecord:
    """校验并持久化一条可发送给浏览器的事件。"""

    try:
        event = stage_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type=event_type,
            payload=payload,
            event_key=event_key,
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
    event_key: str | None = None,
) -> WorkspaceEventRecord:
    """校验并暂存事件，由调用方与其他业务事实一起提交。"""

    safe_payload = validate_event_payload(event_type, payload)
    workspace = _load_workspace(session, workspace_token)
    event = WorkspaceEventRecord(
        workspace_id=workspace.id,
        event_type=event_type,
        event_key=event_key,
        payload=safe_payload,
    )
    session.add(event)
    return event


def lock_workspace_for_turn(
    session: Session,
    *,
    workspace_token: str,
) -> WorkspaceRecord:
    """锁定当前 Workspace 行，保证终态检查与写入在同一事务内。"""

    workspace = session.scalar(
        select(WorkspaceRecord)
        .where(WorkspaceRecord.token_hash == hash_workspace_token(workspace_token))
        .with_for_update()
    )
    if workspace is None:
        raise EventWorkspaceNotFoundError(workspace_token)
    return workspace


def append_turn_started(
    session: Session,
    *,
    workspace_token: str,
    turn_id: str,
) -> WorkspaceEventRecord:
    """锁定 Workspace 并幂等写入当前轮 started 事实。"""

    workspace = lock_workspace_for_turn(session, workspace_token=workspace_token)
    existing = session.scalar(
        select(WorkspaceEventRecord)
        .where(
            WorkspaceEventRecord.workspace_id == workspace.id,
            WorkspaceEventRecord.event_type == "turn.started",
            WorkspaceEventRecord.payload["turn_id"].astext == turn_id,
        )
        .order_by(WorkspaceEventRecord.id)
    )
    if existing is not None:
        session.commit()
        session.refresh(existing)
        return existing
    starts = session.scalars(
        select(WorkspaceEventRecord)
        .where(
            WorkspaceEventRecord.workspace_id == workspace.id,
            WorkspaceEventRecord.event_type == "turn.started",
        )
        .order_by(WorkspaceEventRecord.id.desc())
    ).all()
    now = datetime.now(UTC)
    for started in starts:
        active_id = str(started.payload.get("turn_id", ""))
        if not active_id or active_id == turn_id:
            continue
        lease_raw = started.payload.get("lease_expires_at")
        try:
            lease = (
                datetime.fromisoformat(lease_raw.replace("Z", "+00:00"))
                if isinstance(lease_raw, str)
                else None
            )
        except ValueError:
            lease = None
        if lease is None:
            lease = started.created_at + timedelta(minutes=5)
        if lease <= now:
            continue
        if find_turn_terminal(session, workspace_id=workspace.id, turn_id=active_id) is None:
            session.rollback()
            raise TurnInProgressError("当前 Workspace 已有进行中的对话轮")
    event = WorkspaceEventRecord(
        workspace_id=workspace.id,
        event_type="turn.started",
        payload=validate_event_payload("turn.started", {
            "turn_id": turn_id,
            "lease_expires_at": now + timedelta(minutes=5),
        }),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def find_turn_terminal(
    session: Session,
    *,
    workspace_id: object,
    turn_id: str,
) -> WorkspaceEventRecord | None:
    """查询当前轮已有终态；调用方应先持有 Workspace 行锁。"""

    return session.scalar(
        select(WorkspaceEventRecord)
        .where(
            WorkspaceEventRecord.workspace_id == workspace_id,
            WorkspaceEventRecord.event_type.in_(TERMINAL_EVENT_TYPES),
            WorkspaceEventRecord.payload["turn_id"].astext == turn_id,
        )
        .order_by(WorkspaceEventRecord.id)
    )


def append_turn_terminal(
    session: Session,
    *,
    workspace_token: str,
    turn_id: str,
    event_type: str,
    payload: dict[str, object],
) -> WorkspaceEventRecord | None:
    """在 Workspace 行锁内幂等写入 completed/error/interrupted 之一。"""

    if event_type not in TERMINAL_EVENT_TYPES:
        raise UnsafeEventError("不是合法的对话终态事件")
    workspace = lock_workspace_for_turn(session, workspace_token=workspace_token)
    existing = find_turn_terminal(
        session,
        workspace_id=workspace.id,
        turn_id=turn_id,
    )
    if existing is not None:
        session.commit()
        session.refresh(existing)
        return None
    safe_payload = validate_event_payload(event_type, payload)
    event = WorkspaceEventRecord(
        workspace_id=workspace.id,
        event_type=event_type,
        payload=safe_payload,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def list_turn_events(
    session: Session,
    *,
    workspace_token: str,
    turn_id: str,
) -> list[WorkspaceEventRecord]:
    """返回当前轮已持久化事实，严格按数据库 ID 排序。"""

    workspace = _load_workspace(session, workspace_token)
    return list(
        session.scalars(
            select(WorkspaceEventRecord)
            .where(
                WorkspaceEventRecord.workspace_id == workspace.id,
                WorkspaceEventRecord.payload["turn_id"].astext == turn_id,
            )
            .order_by(WorkspaceEventRecord.id)
        ).all()
    )


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
        retry_consumed=workspace.model_retry_consumed,
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
    is_retry: bool = False,
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
    if is_retry:
        workspace.model_retry_consumed += 1
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(workspace)
    return _quota(workspace)
