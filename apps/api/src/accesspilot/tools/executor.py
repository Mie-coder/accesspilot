"""只读工具白名单；工具参数不允许模型提供身份。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from accesspilot.agent.routing import ConversationIntent
from accesspilot.tools.catalog import (
    ToolResult,
    get_latest_request_status,
    list_active_access,
    list_eligible_access,
)

ReadOnlyToolName = Literal[
    "list_eligible_access",
    "list_active_access",
    "get_latest_request_status",
]


class ReadOnlyToolCall(BaseModel):
    """执行器只接收白名单工具名，Workspace 由服务端另行注入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: ReadOnlyToolName


class UnknownReadOnlyToolError(ValueError):
    """执行器收到绕过 Schema 构造的未知工具时失败闭合。"""


def tool_call_for_intent(intent: ConversationIntent) -> ReadOnlyToolCall | None:
    """只为已实现的查询意图返回工具，写操作永不在白名单中。"""

    mapping: dict[str, ReadOnlyToolName] = {
        "discover_eligible_access": "list_eligible_access",
        "list_active_access": "list_active_access",
        "request_status": "get_latest_request_status",
    }
    tool = mapping.get(intent)
    return ReadOnlyToolCall(tool=tool) if tool is not None else None


def execute_read_only_tool(
    session: Session,
    *,
    workspace_token: str,
    call: ReadOnlyToolCall,
) -> ToolResult:
    """从服务端 Workspace 注入身份，且只执行确定性读工具。"""

    if call.tool == "list_eligible_access":
        return list_eligible_access(session, workspace_token=workspace_token)
    if call.tool == "list_active_access":
        return list_active_access(session, workspace_token=workspace_token)
    if call.tool == "get_latest_request_status":
        return get_latest_request_status(session, workspace_token=workspace_token)
    raise UnknownReadOnlyToolError("工具不在只读白名单中")
