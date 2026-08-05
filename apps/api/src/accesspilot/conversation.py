"""模型配额保护下的申请对话、草稿合并与安全事件写入。"""

import re

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.state import ConversationPhase
from accesspilot.agent.structured_reply import (
    ReplyParsingFailed,
    StructuredReplyModel,
    parse_reply_with_retry,
)
from accesspilot.domain.models import ParsedReply, RequestDraft
from accesspilot.events import (
    ModelQuota,
    append_workspace_event,
    consume_model_call,
)
from accesspilot.tools.catalog import validate_access_request
from accesspilot.workspaces import WorkspaceService


class ConversationInputError(ValueError):
    """聊天消息不满足最小输入要求。"""


class ConversationTurn(BaseModel):
    """对话 API 返回给 assistant-ui 适配器的一轮安全结果。"""

    model_config = ConfigDict(extra="forbid")

    assistant_message: str
    draft: RequestDraft
    missing_fields: list[str]
    phase: ConversationPhase
    business_status: str
    quota: ModelQuota


class DeterministicStructuredReplyModel:
    """无 Key 时使用的透明离线字段提取器。"""

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        employee = re.search(r"\bEMP-\d+\b", user_reply, flags=re.IGNORECASE)
        entitlement = re.search(
            r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*\b",
            user_reply,
            flags=re.IGNORECASE,
        )
        duration = re.search(r"(\d+)\s*天", user_reply)
        justification = re.search(r"(?:用于|为了)([^，。,.]+)", user_reply)
        normalized = user_reply.casefold()
        confirmed: bool | None = None
        if any(marker in normalized for marker in ("不确认", "暂不确认", "不要提交")):
            confirmed = False
        elif any(marker in normalized for marker in ("确认提交", "确认申请", "我确认")):
            confirmed = True
        return ParsedReply(
            employee_id=employee.group(0).upper() if employee else None,
            entitlement_id=entitlement.group(0).lower() if entitlement else None,
            duration_days=int(duration.group(1)) if duration else None,
            justification=justification.group(1).strip() if justification else None,
            confirmed=confirmed,
        )


def _append_event(
    session_factory: sessionmaker[Session],
    *,
    workspace_token: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    with session_factory() as session:
        append_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type=event_type,
            payload=payload,
        )


def _merge_reply(current: RequestDraft, reply: ParsedReply) -> RequestDraft:
    current_data = current.model_dump(mode="json")
    updates = reply.model_dump(mode="json", exclude_none=True)
    business_updates = {
        key for key in updates if key != "confirmed" and updates[key] != current_data[key]
    }
    # 旧确认只对应旧字段；业务内容变化后必须在本轮重新明确确认。
    if business_updates and reply.confirmed is None:
        updates["confirmed"] = False
    return RequestDraft.model_validate({**current_data, **updates})


def _missing_field_question(field_name: str) -> str:
    questions = {
        "employee_id": "请提供你的虚构员工编号，例如 EMP-001。",
        "entitlement_id": "请提供要申请的权限编号，例如 insighthub.customer_export。",
        "duration_days": "请提供申请期限（天）。",
        "justification": "请说明申请这项权限的业务理由。",
    }
    return questions[field_name]


def handle_chat_message(
    session_factory: sessionmaker[Session],
    *,
    workspace_service: WorkspaceService,
    workspace_token: str,
    content: str,
    model: StructuredReplyModel,
) -> ConversationTurn:
    """消费一次模型额度，合并草稿并写入白名单事件。"""

    normalized_content = content.strip()
    if not normalized_content:
        raise ConversationInputError("消息不能为空")

    with session_factory() as session:
        quota = consume_model_call(session, workspace_token=workspace_token)
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="message.user",
        payload={"content": normalized_content},
    )

    workspace = workspace_service.get(workspace_token)
    current_draft = workspace.draft or RequestDraft()
    try:
        def consume_retry_quota() -> None:
            nonlocal quota
            with session_factory() as retry_session:
                quota = consume_model_call(
                    retry_session,
                    workspace_token=workspace_token,
                )

        parsed = parse_reply_with_retry(
            normalized_content,
            model,
            before_retry=consume_retry_quota,
        )
    except (ReplyParsingFailed, httpx.HTTPError, TimeoutError):
        # 模型和网络错误都失败闭合；不把异常详情、请求头或隐藏推理发给浏览器。
        assistant_message = "我暂时没能可靠理解这条消息，请稍后重试或换一种说法。"
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="error.recoverable",
            payload={
                "code": "MODEL_REPLY_UNAVAILABLE",
                "message": assistant_message,
            },
        )
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="message.assistant",
            payload={"content": assistant_message},
        )
        return ConversationTurn(
            assistant_message=assistant_message,
            draft=current_draft,
            missing_fields=current_draft.missing_fields(),
            phase=ConversationPhase.RECOVERABLE_ERROR,
            business_status="recoverable_error",
            quota=quota,
        )

    draft = _merge_reply(current_draft, parsed)
    workspace_service.save_draft(workspace_token, draft)
    missing_fields = draft.missing_fields()
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="draft.updated",
        payload={
            "draft": draft.model_dump(mode="json"),
            "missing_fields": missing_fields,
            "can_enter_approval": draft.can_enter_approval(),
        },
    )

    if missing_fields:
        phase = ConversationPhase.COLLECTING
        business_status = "collecting"
        assistant_message = _missing_field_question(missing_fields[0])
    else:
        with session_factory() as session:
            validation = validate_access_request(session, draft)
        summary = (
            "申请字段与目录校验通过"
            if validation.status == "success"
            else "申请未通过目录校验，请检查员工、权限或期限"
        )
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="tool.summary",
            payload={
                "tool": "validate_access_request",
                "status": validation.status,
                "summary": summary,
            },
        )
        if validation.status != "success":
            phase = ConversationPhase.RECOVERABLE_ERROR
            business_status = "validation_failed"
            assistant_message = summary
        elif draft.confirmed:
            phase = ConversationPhase.AWAITING_CONFIRMATION
            business_status = "ready_to_submit"
            assistant_message = "申请信息已明确确认，可以提交正式申请。"
        else:
            phase = ConversationPhase.AWAITING_CONFIRMATION
            business_status = "awaiting_confirmation"
            assistant_message = "申请信息已完整。请明确回复“确认提交”后再创建正式申请。"

    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="business.status",
        payload={"status": business_status},
    )
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="message.assistant",
        payload={"content": assistant_message},
    )
    return ConversationTurn(
        assistant_message=assistant_message,
        draft=draft,
        missing_fields=missing_fields,
        phase=phase,
        business_status=business_status,
        quota=quota,
    )
