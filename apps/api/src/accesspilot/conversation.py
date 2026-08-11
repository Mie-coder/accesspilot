"""模型配额保护下的申请对话、草稿合并与安全事件写入。"""

import re
from contextvars import ContextVar

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.routing import (
    ConversationIntent,
    DeterministicIntentRouter,
    IntentRoute,
    IntentRouter,
    IntentRoutingFailed,
    route_with_validation,
)
from accesspilot.agent.state import ConversationPhase
from accesspilot.agent.structured_reply import (
    ReplyParsingFailed,
    StructuredReplyModel,
    parse_reply_with_retry,
)
from accesspilot.db.models import EntitlementRecord
from accesspilot.domain.models import ParsedReply, RequestDraft
from accesspilot.events import (
    ModelQuota,
    append_workspace_event,
    consume_model_call,
    get_model_quota,
    validate_event_payload,
)
from accesspilot.tools.catalog import ToolResult, validate_access_request
from accesspilot.tools.executor import (
    ReadOnlyToolCall,
    execute_read_only_tool,
    tool_call_for_intent,
)
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import (
    CursorConflictError,
    DraftRevisionConflictError,
    WorkspaceService,
)


class ConversationInputError(ValueError):
    """聊天消息不满足最小输入要求。"""


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar(
    "accesspilot_current_turn_id", default=None
)
_PERSIST_TERMINAL: ContextVar[bool] = ContextVar(
    "accesspilot_persist_terminal", default=True
)
_TURN_STARTED: ContextVar[bool] = ContextVar(
    "accesspilot_turn_started", default=False
)


class ConversationTurn(BaseModel):
    """对话 API 返回给 assistant-ui 适配器的一轮安全结果。"""

    model_config = ConfigDict(extra="forbid")

    assistant_message: str
    draft: RequestDraft
    missing_fields: list[str]
    phase: ConversationPhase
    business_status: str
    quota: ModelQuota
    intent: ConversationIntent = "request_access"
    security_flagged: bool = False
    tool_results: list[ToolResult] = Field(default_factory=list)
    draft_revision: int = 0
    error_code: str | None = None


def _explicit_confirmation_from_text(content: str) -> bool | None:
    """确认是写入门控，只从用户明确原文推导，不信任模型布尔值。"""

    normalized = content.casefold()
    if any(marker in normalized for marker in ("不确认", "暂不确认", "不要提交")):
        return False
    if any(marker in normalized for marker in ("确认提交", "确认申请", "我确认")):
        return True
    return None


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
        confirmed = _explicit_confirmation_from_text(user_reply)
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
    # prepare 阶段共享旧业务逻辑，但禁止写入 terminal；流式层在模型回答后
    # 通过 append_turn_terminal 原子写入唯一 completed/error/interrupted。
    if not _PERSIST_TERMINAL.get() and event_type in {
        "message.assistant",
        "error.recoverable",
    }:
        return
    turn_id = _CURRENT_TURN_ID.get()
    event_payload = dict(payload)
    if turn_id is not None:
        event_payload.setdefault("turn_id", turn_id)
    with session_factory() as session:
        append_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type=event_type,
            payload=event_payload,
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


def _entitlement_resolution_message(result: ToolResult) -> str:
    """把解析事实转成不回显未知查询的确定性提示。"""

    resolution = result.entitlement_resolution
    if resolution is None:
        return "当前无法可靠解析权限，请检查演示身份后重试。"
    if resolution.status == "ambiguous":
        choices = "、".join(
            f"{candidate.name}（{candidate.code}）"
            for candidate in resolution.candidates
        )
        return f"检测到多个可能的权限，请明确选择：{choices}。"
    if resolution.status == "no_match":
        choices = "、".join(
            f"{candidate.name}（{candidate.code}）"
            for candidate in resolution.eligible_access
        )
        if choices:
            return f"未能匹配到权限。当前可申请权限：{choices}。"
        return "未能匹配到权限；当前身份暂无可申请权限。"
    return "权限解析成功"


def _entitlement_resolution_status(result: ToolResult) -> str:
    """工具摘要只记录稳定解析状态或外层事实状态。"""

    resolution = result.entitlement_resolution
    return resolution.status if resolution is not None else result.status


SECURITY_MESSAGE = (
    "我不能提供系统提示词、API Key、隐藏推理或帮助绕过权限；可以继续处理公开的权限业务需求。"
)


def _redact_sensitive_content(content: str) -> str:
    """在事件回放与模型输入前隐藏常见密钥形态。"""

    redacted = re.sub(
        r"(?i)\b(?:sk|ds)-[a-z0-9_-]{8,}",
        "[已隐藏疑似密钥]",
        content,
    )
    redacted = re.sub(
        (
            r"(?i)(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|"
            r"refresh[_ -]?token|private[_ -]?key|password)\s*[:=]\s*\S+"
        ),
        "[已隐藏凭证]",
        redacted,
    )
    return re.sub(
        r"(?i)bearer\s+\S+",
        "[已隐藏 Bearer 凭证]",
        redacted,
    )


_PROTECTED_INTERNAL_CONTENT_MARKERS = (
    "你是 accesspilot",
    "只允许 employee_id",
    "json 示例",
    "你只能依据输入中的申请事实",
    "citations 中每项只能包含",
)

_MIN_PROTECTED_PREFIX_LENGTH = 8

_UNSAFE_STRUCTURED_FIELD_MARKERS = (
    "system prompt",
    "developer prompt",
    "developer message",
    "developer instructions",
    "chain of thought",
    "系统提示词",
    "开发者消息",
    "隐藏推理",
)


def contains_protected_internal_content(content: str) -> bool:
    """识别不得进入草稿或回答流的内部指令片段。"""

    normalized = " ".join(content.casefold().split())
    return any(
        marker in normalized for marker in _PROTECTED_INTERNAL_CONTENT_MARKERS
    )


def split_safe_model_output_prefix(pending: str) -> tuple[str, str]:
    """保留可能继续拼成内部指令的后缀，其余文本可立即流出。"""

    for start in range(len(pending)):
        normalized_suffix = " ".join(pending[start:].casefold().split())
        if normalized_suffix and any(
            marker.startswith(normalized_suffix)
            for marker in _PROTECTED_INTERNAL_CONTENT_MARKERS
        ):
            return pending[:start], pending[start:]
    return pending, ""


def is_suspicious_protected_prefix(content: str) -> bool:
    """识别流结束时仍可识别的内部指令前缀。

    流式检查会暂存所有可能继续拼成受保护标记的后缀。极短的自然
    结尾（如“你”）可正常释放；达到可识别长度的前缀必须失败闭合。
    """

    normalized = " ".join(content.casefold().split())
    return len(normalized) >= _MIN_PROTECTED_PREFIX_LENGTH and any(
        marker.startswith(normalized)
        for marker in _PROTECTED_INTERNAL_CONTENT_MARKERS
    )


def _sanitize_model_text(content: str | None) -> str | None:
    """模型字符串既要脱敏，也不得把内部提示复述进业务事实。"""

    if content is None:
        return None
    redacted = _redact_sensitive_content(content)
    normalized = " ".join(redacted.casefold().split())
    if contains_protected_internal_content(redacted) or any(
        marker in normalized for marker in _UNSAFE_STRUCTURED_FIELD_MARKERS
    ):
        return None
    return redacted


def _is_request_collection_follow_up(
    content: str,
    missing_fields: list[str],
) -> bool:
    """只把能对应下一缺失字段的短句当作申请续答。"""

    if not missing_fields:
        return False
    normalized = content.casefold().strip()
    next_field = missing_fields[0]
    if next_field == "entitlement_id":
        return any(
            marker in normalized for marker in ("客户导出", "看板查看", "代码仓库", "仓库只读")
        )
    if next_field == "duration_days":
        return re.search(r"\d+\s*天", normalized) is not None
    if next_field == "justification":
        return any(
            marker in normalized
            for marker in (
                "因为",
                "用于",
                "为了",
                "核对",
                "排查",
                "分析",
                "开发",
                "测试",
                "运营",
                "审计",
                "交付",
                "项目",
                "业务",
                "数据",
            )
        )
    return False


def _tool_answer(route: IntentRoute, result: ToolResult | None) -> str:
    """用确定性模板把工具事实转成用户可读回答。"""

    if route.intent == "policy_question":
        if result is not None and result.policy_catalog is not None:
            items = "；".join(
                f"{item.policy_code}《{item.title}》" for item in result.policy_catalog
            )
            return f"当前基本政策共 {len(result.policy_catalog)} 条：{items}。"
        if result is not None and result.policy_answer is not None:
            return result.policy_answer.answer
        return "政策事实源暂时不可用，当前无法提供可靠依据。"
    if result is not None and result.status not in {"success", "request_not_found"}:
        return "当前无法在后端事实源中完成查询，请检查演示身份后重试。"
    if route.intent == "discover_eligible_access" and result is not None:
        eligible_items = result.eligible_access or []
        if not eligible_items:
            return "根据当前演示身份，暂时没有可以自助申请的权限。"
        names = "、".join(item.name for item in eligible_items)
        return f"根据当前演示身份，你可以申请 {len(eligible_items)} 项权限：{names}。"
    if route.intent == "list_active_access" and result is not None:
        active_items = result.active_access or []
        if not active_items:
            return "当前演示身份没有仍在有效期内的已开通权限。"
        names = "、".join(item.name for item in active_items)
        return f"当前有效授权共 {len(active_items)} 项：{names}。"
    if route.intent == "request_status" and result is not None:
        status = result.request_status
        if status is None:
            return "当前演示身份还没有可查询的正式申请。"
        approval = status.approval_status or "尚未启动审批"
        granted = "已开通" if status.access_granted else "未开通"
        return f"最近申请状态为 {status.request_status}，审批为 {approval}，权限{granted}。"
    if route.intent == "security_probe":
        return SECURITY_MESSAGE
    if route.intent == "unknown":
        return "我需要更多上下文才能理解这条数字消息，请说明它是期限、权限编号还是其他内容。"
    return "我可以帮你查询可申请权限、当前有效授权、申请状态，或发起权限申请。"


def _append_security_notice(
    session_factory: sessionmaker[Session],
    *,
    workspace_token: str,
) -> None:
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="security.notice",
        payload={
            "code": "SENSITIVE_INTERNAL_REQUEST",
            "message": SECURITY_MESSAGE,
        },
    )


_NUMERIC_INPUT_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_VALID_DURATION_RE = re.compile(r"[1-9][0-9]{0,3}")
_CURSOR_BREAKING_INTENTS = frozenset(
    {
        "help",
        "policy_question",
        "discover_eligible_access",
        "list_active_access",
        "request_status",
    }
)


def _is_numeric_input(content: str) -> bool:
    """识别 T18 需要服务端上下文解释的数字型短输入。"""

    return _NUMERIC_INPUT_RE.fullmatch(content) is not None


def _numeric_duration(content: str) -> int | None:
    """仅接受规范的无前导零、最多四位正整数期限候选。"""

    if _VALID_DURATION_RE.fullmatch(content) is None:
        return None
    return int(content)


def _apply_cursor_transition(
    workspace_service: WorkspaceService,
    *,
    workspace_token: str,
    turn: ConversationTurn,
) -> None:
    """在成功终态后激活下一追问；流中断时由调用方不执行此函数。"""

    if turn.intent in _CURSOR_BREAKING_INTENTS:
        # 明确切出申请收集的问题不能让旧 Cursor 继续解释下一轮数字。
        workspace_service.clear_cursor(workspace_token)
        return
    if turn.intent != "request_access":
        return
    if turn.business_status == "collecting" and turn.missing_fields:
        expected_field = turn.missing_fields[0]
        workspace_service.activate_cursor(
            workspace_token,
            expected_revision=turn.draft_revision,
            expected_field=expected_field,
            last_question_kind=expected_field,
        )
    elif turn.business_status == "awaiting_confirmation":
        workspace_service.activate_cursor(
            workspace_token,
            expected_revision=turn.draft_revision,
            expected_field="confirmation",
            last_question_kind="confirmation",
        )
    elif turn.business_status not in {"recoverable_error", "validation_failed"}:
        workspace_service.clear_cursor(workspace_token)


def apply_cursor_transition(
    workspace_service: WorkspaceService,
    *,
    workspace_token: str,
    turn: ConversationTurn,
) -> None:
    """公开给 SSE 终态收尾使用的 Cursor 状态转换。"""

    _apply_cursor_transition(
        workspace_service,
        workspace_token=workspace_token,
        turn=turn,
    )


def normalized_outcome(turn: ConversationTurn) -> dict[str, object]:
    """返回 JSON 与 SSE terminal 共用的最小 Outcome 合同。"""

    outcome: dict[str, object] = {
        "intent": turn.intent,
        "business_status": turn.business_status,
        "draft_revision": turn.draft_revision,
        "draft": turn.draft.model_dump(mode="json"),
        "assistant_message": turn.assistant_message,
    }
    if turn.error_code is not None:
        outcome["error_code"] = turn.error_code
    return outcome


def _append_outcome_events(
    session_factory: sessionmaker[Session],
    *,
    workspace_token: str,
    turn_id: str | None,
    assistant_message: str,
    business_status: str,
) -> None:
    """为确定性数字续答写入与旧 JSON 入口一致的安全事件。"""

    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="business.status",
        payload={
            "status": business_status,
            **({"turn_id": turn_id} if turn_id is not None else {}),
        },
    )
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="message.assistant",
        payload={
            "content": assistant_message,
            **({"turn_id": turn_id} if turn_id is not None else {}),
        },
    )


def _numeric_follow_up(
    session_factory: sessionmaker[Session],
    *,
    workspace_service: WorkspaceService,
    workspace_token: str,
    content: str,
) -> ConversationTurn:
    """不调用模型地处理纯数字输入，并按活动 Cursor 选择语义。"""

    workspace = workspace_service.get(workspace_token)
    cursor = workspace.active_cursor()
    visible_draft = workspace.draft
    if (
        visible_draft is not None
        and visible_draft.employee_id is not None
        and visible_draft.employee_id != workspace.actor_id
    ):
        visible_draft = None
    draft = visible_draft or RequestDraft(employee_id=workspace.actor_id)
    with session_factory() as session:
        quota = get_model_quota(session, workspace_token=workspace_token)

    turn_id = _CURRENT_TURN_ID.get()
    if turn_id is not None:
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="intent.detected",
            payload={
                "turn_id": turn_id,
                "intent": "request_access" if cursor is not None else "unknown",
                "security_flagged": False,
            },
        )
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="message.user",
        payload={"content": content, **({"turn_id": turn_id} if turn_id else {})},
    )

    if cursor is None:
        assistant_message = "我需要更多上下文才能理解“111”：它是期限、权限编号，还是其他内容？"
        business_status = "needs_clarification"
        turn = ConversationTurn(
            assistant_message=assistant_message,
            draft=draft,
            missing_fields=draft.missing_fields(),
            phase=(
                ConversationPhase.COLLECTING
                if draft.missing_fields()
                else ConversationPhase.AWAITING_CONFIRMATION
            ),
            business_status=business_status,
            quota=quota,
            intent="unknown",
            draft_revision=workspace.draft_revision,
            error_code="NUMERIC_CONTEXT_REQUIRED",
        )
        _append_outcome_events(
            session_factory,
            workspace_token=workspace_token,
            turn_id=turn_id,
            assistant_message=assistant_message,
            business_status=business_status,
        )
        return turn

    expected_field = cursor.expected_field
    error_code: str | None = None
    candidate = _numeric_duration(content) if expected_field == "duration_days" else None
    updated_workspace = workspace

    if expected_field != "duration_days":
        questions = {
            "entitlement_id": "请提供权限名称或权限编号，例如 insighthub.customer_export。",
            "justification": "请说明申请这项权限的业务理由。",
            "confirmation": "请明确回复“确认提交”或继续修改申请信息。",
            "none": "请说明你要办理的权限业务。",
        }
        assistant_message = questions[expected_field]
        business_status = (
            "awaiting_confirmation" if expected_field == "confirmation" else "collecting"
        )
        error_code = {
            "entitlement_id": "ENTITLEMENT_REQUIRED",
            "justification": "JUSTIFICATION_REQUIRED",
            "confirmation": "CONFIRMATION_REQUIRED",
            "none": "NUMERIC_CONTEXT_REQUIRED",
        }[expected_field]
    elif candidate is None:
        assistant_message = "申请期限必须是 1–9999 天的正整数，请重新输入期限。"
        business_status = "collecting"
        error_code = "INVALID_DURATION_DAYS"
    else:
        maximum: int | None = None
        if draft.entitlement_id is not None:
            with session_factory() as session:
                entitlement = session.get(EntitlementRecord, draft.entitlement_id)
            maximum = entitlement.max_duration_days if entitlement is not None else None
        if maximum is not None and candidate > maximum:
            assistant_message = (
                f"该权限最长只能申请 {maximum} 天，请重新输入不超过上限的期限。"
            )
            business_status = "collecting"
            error_code = "DURATION_EXCEEDS_MAXIMUM"
        else:
            proposed = draft.model_copy(
                update={"duration_days": candidate, "confirmed": False}
            )
            try:
                updated_workspace = workspace_service.consume_cursor_cas(
                    workspace_token,
                    expected_revision=cursor.draft_revision,
                    expected_field="duration_days",
                    draft=proposed,
                )
            except CursorConflictError:
                assistant_message = "这条期限上下文已经变化，请重新说明申请内容。"
                business_status = "needs_clarification"
                error_code = "CURSOR_STALE"
                turn = ConversationTurn(
                    assistant_message=assistant_message,
                    draft=draft,
                    missing_fields=draft.missing_fields(),
                    phase=ConversationPhase.COLLECTING,
                    business_status=business_status,
                    quota=quota,
                    intent="unknown",
                    draft_revision=workspace.draft_revision,
                    error_code=error_code,
                )
                _append_outcome_events(
                    session_factory,
                    workspace_token=workspace_token,
                    turn_id=turn_id,
                    assistant_message=assistant_message,
                    business_status=business_status,
                )
                return turn

            assert updated_workspace.draft is not None
            draft = updated_workspace.draft
            draft_revision = updated_workspace.draft_revision
            draft_event_payload: dict[str, object] = {
                "draft": draft.model_dump(mode="json"),
                "missing_fields": draft.missing_fields(),
                "can_enter_approval": draft.can_enter_approval(),
                "draft_revision": draft_revision,
            }
            validate_event_payload("draft.updated", draft_event_payload)
            _append_event(
                session_factory,
                workspace_token=workspace_token,
                event_type="draft.updated",
                payload=draft_event_payload,
            )
            missing_fields = draft.missing_fields()
            if missing_fields:
                business_status = "collecting"
                assistant_message = _missing_field_question(missing_fields[0])
            else:
                with session_factory() as session:
                    validation = validate_access_request(session, draft)
                if validation.status != "success":
                    business_status = "validation_failed"
                    assistant_message = "申请未通过目录校验，请检查员工、权限或期限。"
                    error_code = "BUSINESS_VALIDATION_FAILED"
                elif draft.confirmed:
                    business_status = "ready_to_submit"
                    assistant_message = "申请信息已明确确认，可以提交正式申请。"
                else:
                    business_status = "awaiting_confirmation"
                    assistant_message = (
                        "申请信息已完整。请明确回复“确认提交”后再创建正式申请。"
                    )
            turn = ConversationTurn(
                assistant_message=assistant_message,
                draft=draft,
                missing_fields=draft.missing_fields(),
                phase=(
                    ConversationPhase.COLLECTING
                    if business_status == "collecting"
                    else ConversationPhase.AWAITING_CONFIRMATION
                ),
                business_status=business_status,
                quota=quota,
                intent="request_access",
                draft_revision=draft_revision,
                error_code=error_code,
            )
            _append_outcome_events(
                session_factory,
                workspace_token=workspace_token,
                turn_id=turn_id,
                assistant_message=assistant_message,
                business_status=business_status,
            )
            if _PERSIST_TERMINAL.get():
                _apply_cursor_transition(
                    workspace_service,
                    workspace_token=workspace_token,
                    turn=turn,
                )
            return turn

    turn = ConversationTurn(
        assistant_message=assistant_message,
        draft=draft,
        missing_fields=draft.missing_fields(),
        phase=(
            ConversationPhase.AWAITING_CONFIRMATION
            if business_status == "awaiting_confirmation"
            else ConversationPhase.COLLECTING
        ),
        business_status=business_status,
        quota=quota,
        intent="request_access",
        draft_revision=updated_workspace.draft_revision,
        error_code=error_code,
    )
    _append_outcome_events(
        session_factory,
        workspace_token=workspace_token,
        turn_id=turn_id,
        assistant_message=assistant_message,
        business_status=business_status,
    )
    return turn


def _process_chat_message(
    session_factory: sessionmaker[Session],
    *,
    workspace_service: WorkspaceService,
    workspace_token: str,
    content: str,
    model: StructuredReplyModel,
    router: IntentRouter | None = None,
    policy_service: PolicyService | None = None,
) -> ConversationTurn:
    """先路由再执行；只有申请意图消费模型额度并修改草稿。"""

    normalized_content = content.strip()
    if not normalized_content:
        raise ConversationInputError("消息不能为空")

    workspace = workspace_service.get(workspace_token)
    entry_draft_revision = workspace.draft_revision
    active_policy_service = policy_service or PolicyService(
        embedding_model=DeterministicEmbeddingModel()
    )
    turn_id = _CURRENT_TURN_ID.get()
    if turn_id is not None and not _TURN_STARTED.get():
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="turn.started",
            payload={"turn_id": turn_id},
        )
    visible_draft = workspace.draft
    if (
        visible_draft is not None
        and visible_draft.employee_id is not None
        and visible_draft.employee_id != workspace.actor_id
    ):
        visible_draft = None
    active_router = router or DeterministicIntentRouter()
    try:
        route = route_with_validation(normalized_content, active_router)
    except IntentRoutingFailed as error:
        raise ConversationInputError("暂时无法可靠识别该请求意图") from error
    if (
        route.intent == "help"
        and visible_draft is not None
        and _is_request_collection_follow_up(
            normalized_content,
            visible_draft.missing_fields(),
        )
    ):
        # 已经进入申请收集时，“做数据核对”这类简短回答是当前缺失字段。
        route = IntentRoute(
            intent="request_access",
            security_probe=route.security_probe,
        )
    if route.intent in _CURSOR_BREAKING_INTENTS:
        # 显式换题一经可靠路由就立即失效旧 Cursor；流式回答即使
        # 随后中断/报错也不能让旧期限解释下一轮数字。
        workspace_service.clear_cursor(workspace_token)
    # 纯数字输入必须在模型配额和结构化解析之前闭合；活动 Cursor 才能
    # 将其提升为申请续答，否则固定返回 unknown/needs_clarification。
    if _is_numeric_input(normalized_content):
        return _numeric_follow_up(
            session_factory,
            workspace_service=workspace_service,
            workspace_token=workspace_token,
            content=normalized_content,
        )
    if turn_id is not None:
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="intent.detected",
            payload={
                "turn_id": turn_id,
                "intent": route.intent,
                "security_flagged": route.security_probe,
            },
        )
    safe_content = _redact_sensitive_content(normalized_content)
    current_draft = visible_draft or RequestDraft(employee_id=workspace.actor_id)

    if route.intent != "request_access":
        with session_factory() as session:
            quota = get_model_quota(session, workspace_token=workspace_token)
            call = tool_call_for_intent(route.intent, content=safe_content)
            result = (
                execute_read_only_tool(
                    session,
                    workspace_token=workspace_token,
                    call=call,
                    policy_service=active_policy_service,
                )
                if call is not None
                else None
            )
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="message.user",
            payload={"content": safe_content},
        )
        if route.security_probe:
            _append_security_notice(
                session_factory,
                workspace_token=workspace_token,
            )
        assistant_message = _tool_answer(route, result)
        if route.security_probe and route.intent != "security_probe":
            assistant_message = f"{SECURITY_MESSAGE}\n\n{assistant_message}"
        if call is not None and result is not None:
            _append_event(
                session_factory,
                workspace_token=workspace_token,
                event_type="tool.summary",
                payload={
                    "tool": call.tool,
                    "status": result.status,
                    "summary": assistant_message,
                },
            )
        non_request_status = (
            "needs_clarification" if route.intent == "unknown" else "answered"
        )
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="business.status",
            payload={"status": non_request_status},
        )
        _append_event(
            session_factory,
            workspace_token=workspace_token,
            event_type="message.assistant",
            payload={"content": assistant_message},
        )
        phase = (
            ConversationPhase.COLLECTING
            if current_draft.missing_fields()
            else ConversationPhase.AWAITING_CONFIRMATION
        )
        turn = ConversationTurn(
            assistant_message=assistant_message,
            draft=current_draft,
            missing_fields=current_draft.missing_fields(),
            phase=phase,
            business_status=non_request_status,
            quota=quota,
            intent=route.intent,
            security_flagged=route.security_probe,
            tool_results=[result] if result is not None else [],
            draft_revision=workspace.draft_revision,
            error_code=(
                "NUMERIC_CONTEXT_REQUIRED" if route.intent == "unknown" else None
            ),
        )
        if _PERSIST_TERMINAL.get():
            _apply_cursor_transition(
                workspace_service,
                workspace_token=workspace_token,
                turn=turn,
            )
        return turn

    if (
        workspace.draft is not None
        and workspace.draft.employee_id is not None
        and workspace.draft.employee_id != workspace.actor_id
    ):
        # 切换角色不能静默把旧申请人改成新身份。
        raise ConversationInputError("当前草稿属于另一演示身份，请先切回原身份")

    with session_factory() as session:
        quota = consume_model_call(session, workspace_token=workspace_token)
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="message.user",
        payload={"content": safe_content},
    )
    if route.security_probe:
        _append_security_notice(
            session_factory,
            workspace_token=workspace_token,
        )

    entitlement_resolution_result: ToolResult | None = None
    try:

        def consume_retry_quota() -> None:
            nonlocal quota
            with session_factory() as retry_session:
                quota = consume_model_call(
                    retry_session,
                    workspace_token=workspace_token,
                    is_retry=True,
                )

        parsed = parse_reply_with_retry(
            safe_content,
            model,
            before_retry=consume_retry_quota,
        )
        explicit_confirmation = _explicit_confirmation_from_text(normalized_content)
        if route.security_probe and explicit_confirmation is None:
            explicit_confirmation = False
        # 模型可以理解用户文本，但无权更改身份事实或把疑似密钥写入草稿。
        parsed = parsed.model_copy(
            update={
                "employee_id": workspace.actor_id,
                "entitlement_id": _sanitize_model_text(parsed.entitlement_id),
                "justification": _sanitize_model_text(parsed.justification),
                "confirmed": explicit_confirmation,
            }
        )
        if parsed.entitlement_id is not None:
            if parsed.entitlement_id.strip():
                with session_factory() as session:
                    entitlement_resolution_result = execute_read_only_tool(
                        session,
                        workspace_token=workspace_token,
                        call=ReadOnlyToolCall(
                            tool="resolve_entitlement",
                            query=parsed.entitlement_id,
                        ),
                    )
            else:
                entitlement_resolution_result = ToolResult(status="invalid_argument")

            assert entitlement_resolution_result is not None
            resolution = entitlement_resolution_result.entitlement_resolution
            resolution_status = (
                resolution.status if resolution is not None else None
            )
            matched = (
                entitlement_resolution_result.status == "success"
                and resolution is not None
                and resolution.status == "matched"
                and len(resolution.candidates) == 1
            )
            resolution_summary = _entitlement_resolution_message(
                entitlement_resolution_result
            )
            _append_event(
                session_factory,
                workspace_token=workspace_token,
                event_type="tool.summary",
                payload={
                    "tool": "resolve_entitlement",
                    "status": _entitlement_resolution_status(
                        entitlement_resolution_result
                    ),
                    "summary": resolution_summary,
                },
            )
            if not matched:
                business_status = (
                    f"entitlement_{resolution_status}"
                    if resolution_status in {"ambiguous", "no_match"}
                    else "resolution_unavailable"
                )
                assistant_message = resolution_summary
                if route.security_probe:
                    assistant_message = f"{SECURITY_MESSAGE}\n\n{assistant_message}"
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
                missing_fields = current_draft.missing_fields()
                phase = (
                    ConversationPhase.COLLECTING
                    if missing_fields
                    else ConversationPhase.AWAITING_CONFIRMATION
                )
                return ConversationTurn(
                    assistant_message=assistant_message,
                    draft=current_draft,
                    missing_fields=missing_fields,
                    phase=phase,
                    business_status=business_status,
                    quota=quota,
                    intent=route.intent,
                    security_flagged=route.security_probe,
                    tool_results=[entitlement_resolution_result],
                    draft_revision=workspace.draft_revision,
                )

            assert resolution is not None
            parsed = parsed.model_copy(
                update={"entitlement_id": resolution.candidates[0].code}
            )
    except (ReplyParsingFailed, httpx.HTTPError, TimeoutError):
        # 模型和网络错误都失败闭合；不把异常详情、请求头或隐藏推理发给浏览器。
        assistant_message = "我暂时没能可靠理解这条消息，请稍后重试或换一种说法。"
        if route.security_probe:
            assistant_message = f"{SECURITY_MESSAGE}\n\n{assistant_message}"
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
            intent=route.intent,
            security_flagged=route.security_probe,
            draft_revision=workspace.draft_revision,
            error_code="MODEL_REPLY_UNAVAILABLE",
        )

    draft = _merge_reply(current_draft, parsed)
    missing_fields = draft.missing_fields()
    draft_changed = workspace.draft is None or workspace.draft != draft
    if draft_changed:
        try:
            updated_workspace = workspace_service.save_draft_cas(
                workspace_token,
                expected_revision=entry_draft_revision,
                draft=draft,
            )
        except DraftRevisionConflictError:
            # The model parsed an older snapshot while another request advanced
            # the draft. Keep the newer fact and close this turn safely.
            latest_workspace = workspace_service.get(workspace_token)
            latest_draft = latest_workspace.draft
            if (
                latest_draft is not None
                and latest_draft.employee_id is not None
                and latest_draft.employee_id != latest_workspace.actor_id
            ):
                latest_draft = None
            safe_draft = latest_draft or RequestDraft(
                employee_id=latest_workspace.actor_id
            )
            safe_missing_fields = safe_draft.missing_fields()
            conflict_message = (
                "申请草稿刚刚被另一轮更新，请基于最新草稿继续。"
            )
            _append_event(
                session_factory,
                workspace_token=workspace_token,
                event_type="business.status",
                payload={"status": "recoverable_error"},
            )
            _append_event(
                session_factory,
                workspace_token=workspace_token,
                event_type="message.assistant",
                payload={"content": conflict_message},
            )
            return ConversationTurn(
                assistant_message=conflict_message,
                draft=safe_draft,
                missing_fields=safe_missing_fields,
                phase=ConversationPhase.RECOVERABLE_ERROR,
                business_status="recoverable_error",
                quota=quota,
                intent=route.intent,
                security_flagged=route.security_probe,
                tool_results=(
                    [entitlement_resolution_result]
                    if entitlement_resolution_result is not None
                    else []
                ),
                draft_revision=latest_workspace.draft_revision,
                error_code="DRAFT_REVISION_CONFLICT",
            )
    else:
        updated_workspace = workspace
    draft_revision = updated_workspace.draft_revision
    draft_event_payload: dict[str, object] = {
        "draft": draft.model_dump(mode="json"),
        "missing_fields": missing_fields,
        "can_enter_approval": draft.can_enter_approval(),
        "draft_revision": draft_revision,
    }
    # 先通过前端事件安全边界，再持久化同一份草稿，避免失败后留下敏感残留。
    validate_event_payload("draft.updated", draft_event_payload)
    _append_event(
        session_factory,
        workspace_token=workspace_token,
        event_type="draft.updated",
        payload=draft_event_payload,
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

    if route.security_probe:
        assistant_message = f"{SECURITY_MESSAGE}\n\n{assistant_message}"

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
    turn = ConversationTurn(
        assistant_message=assistant_message,
        draft=draft,
        missing_fields=missing_fields,
        phase=phase,
        business_status=business_status,
        quota=quota,
        intent=route.intent,
        security_flagged=route.security_probe,
        tool_results=(
            [entitlement_resolution_result]
            if entitlement_resolution_result is not None
            else []
        ),
        draft_revision=draft_revision,
    )
    if _PERSIST_TERMINAL.get():
        _apply_cursor_transition(
            workspace_service,
            workspace_token=workspace_token,
            turn=turn,
        )
    return turn


def handle_chat_message(
    session_factory: sessionmaker[Session],
    *,
    workspace_service: WorkspaceService,
    workspace_token: str,
    content: str,
    model: StructuredReplyModel,
    router: IntentRouter | None = None,
    policy_service: PolicyService | None = None,
) -> ConversationTurn:
    """旧 JSON 入口：保留完整 terminal 事件和原有返回合同。"""

    return _process_chat_message(
        session_factory,
        workspace_service=workspace_service,
        workspace_token=workspace_token,
        content=content,
        model=model,
        router=router,
        policy_service=policy_service,
    )


def prepare_chat_message(
    session_factory: sessionmaker[Session],
    *,
    workspace_service: WorkspaceService,
    workspace_token: str,
    content: str,
    model: StructuredReplyModel,
    turn_id: str,
    router: IntentRouter | None = None,
    policy_service: PolicyService | None = None,
) -> ConversationTurn:
    """流式入口第一阶段：复用字段/工具校验，但不写 assistant terminal。"""

    id_token = _CURRENT_TURN_ID.set(turn_id)
    terminal_token = _PERSIST_TERMINAL.set(False)
    started_token = _TURN_STARTED.set(True)
    try:
        return _process_chat_message(
            session_factory,
            workspace_service=workspace_service,
            workspace_token=workspace_token,
            content=content,
            model=model,
            router=router,
            policy_service=policy_service,
        )
    finally:
        _TURN_STARTED.reset(started_token)
        _PERSIST_TERMINAL.reset(terminal_token)
        _CURRENT_TURN_ID.reset(id_token)
