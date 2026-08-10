"""只读工具白名单；工具参数不允许模型提供身份。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.routing import ConversationIntent, classify_policy_question
from accesspilot.tools.catalog import (
    ToolResult,
    get_latest_request_status,
    list_active_access,
    list_eligible_access,
    resolve_entitlement_candidates,
)
from accesspilot.tools.policies import PolicyAnswer, PolicyService

ReadOnlyToolName = Literal[
    "list_eligible_access",
    "list_active_access",
    "get_latest_request_status",
    "resolve_entitlement",
    "list_policy_catalog",
    "get_self_approval_policy",
    "search_policies",
]


class ReadOnlyToolCall(BaseModel):
    """执行器只接收白名单工具名，Workspace 由服务端另行注入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: ReadOnlyToolName
    query: Annotated[StrictStr, Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if self.tool in {"resolve_entitlement", "search_policies"}:
            if self.query is None or not self.query.strip():
                raise ValueError(f"{self.tool} requires a non-empty query")
        elif self.query is not None:
            raise ValueError(
                "query is only allowed for resolve_entitlement or search_policies"
            )
        return self


class UnknownReadOnlyToolError(ValueError):
    """执行器收到绕过 Schema 构造的未知工具时失败闭合。"""


def tool_call_for_intent(
    intent: ConversationIntent,
    content: str | None = None,
) -> ReadOnlyToolCall | None:
    """只为已实现的查询意图返回工具，写操作永不在白名单中。"""

    mapping: dict[str, ReadOnlyToolName] = {
        "discover_eligible_access": "list_eligible_access",
        "list_active_access": "list_active_access",
        "request_status": "get_latest_request_status",
    }
    if intent == "policy_question":
        safe_content = content.strip() if content is not None else ""
        policy_kind = classify_policy_question(safe_content) if safe_content else "catalog"
        if policy_kind == "catalog":
            return ReadOnlyToolCall(tool="list_policy_catalog")
        if policy_kind == "self_approval":
            return ReadOnlyToolCall(tool="get_self_approval_policy")
        return ReadOnlyToolCall(tool="search_policies", query=safe_content)
    tool = mapping.get(intent)
    return ReadOnlyToolCall(tool=tool) if tool is not None else None


def execute_read_only_tool(
    session: Session,
    *,
    workspace_token: str,
    call: ReadOnlyToolCall,
    policy_service: PolicyService | None = None,
) -> ToolResult:
    """从服务端 Workspace 注入身份，且只执行确定性读工具。"""

    if call.tool == "list_policy_catalog":
        active_policy_service = policy_service or PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        )
        try:
            catalog = active_policy_service.catalog(session)
        except Exception:
            return ToolResult(
                status="success",
                policy_answer=PolicyAnswer(
                    status="retrieval_unavailable",
                    answer="政策事实源暂时不可用，当前无法提供完整目录。",
                    evidence=[],
                    next_step="请稍后重试；如问题紧急，请联系人工安全流程。",
                ),
            )
        return ToolResult(
            status="success",
            policy_catalog=catalog,
        )
    if call.tool == "get_self_approval_policy":
        active_policy_service = policy_service or PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        )
        return ToolResult(
            status="success",
            policy_answer=active_policy_service.query(session, "我能自己审批自己的申请吗？"),
        )
    if call.tool == "search_policies":
        if call.query is None or not call.query.strip():
            return ToolResult(status="invalid_argument")
        active_policy_service = policy_service or PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        )
        return ToolResult(
            status="success",
            policy_answer=active_policy_service.query(session, call.query),
        )
    if call.tool == "resolve_entitlement":
        if call.query is None or not call.query.strip():
            return ToolResult(status="invalid_argument")
        eligible_result = list_eligible_access(
            session,
            workspace_token=workspace_token,
        )
        if eligible_result.status != "success" or eligible_result.eligible_access is None:
            return eligible_result
        resolution = resolve_entitlement_candidates(
            eligible_result.eligible_access,
            call.query,
        )
        return ToolResult(status="success", entitlement_resolution=resolution)
    if call.tool == "list_eligible_access":
        return list_eligible_access(session, workspace_token=workspace_token)
    if call.tool == "list_active_access":
        return list_active_access(session, workspace_token=workspace_token)
    if call.tool == "get_latest_request_status":
        return get_latest_request_status(session, workspace_token=workspace_token)
    raise UnknownReadOnlyToolError("工具不在只读白名单中")
