from enum import StrEnum
from typing import TypedDict

from accesspilot.domain.models import RequestDraft


class ConversationPhase(StrEnum):
    """多轮对话中，Agent 当前所处的业务阶段。"""

    # 正在逐项收集申请人尚未提供的草稿字段。
    COLLECTING = "collecting"
    # 草稿字段已完整，等待用户明确确认后才允许送审。
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    # 出现可提示用户修正后继续的错误，不代表申请失败或权限未开通。
    RECOVERABLE_ERROR = "recoverable_error"


class RecoverableError(TypedDict):
    """用户修正输入后，可以继续对话的错误。"""

    code: str
    message: str


class GraphState(TypedDict):
    """一次权限申请对话中需要持续保存的最小状态。"""

    # 随对话逐步补齐的申请草稿，尚不代表审批或授权事实。
    draft: RequestDraft
    # 当前仍缺的业务字段，节点据此决定下一次只追问什么。
    missing_fields: list[str]
    # 工具调用后的简短结论，避免把冗长原始结果重复放入对话上下文。
    tool_summaries: list[str]
    # 当前多轮对话所处的阶段。
    phase: ConversationPhase
    # 最近一次可由用户修正后继续的输入错误；没有错误时为 None。
    recoverable_error: RecoverableError | None
    next_question:str| None

def create_initial_state(draft: RequestDraft) -> GraphState:
    """为一次新的权限申请对话创建初始状态。"""

    return {
        "draft": draft,
        "missing_fields": draft.missing_fields(),
        "tool_summaries": [],
        "phase": ConversationPhase.COLLECTING,
        "recoverable_error": None,
        "next_question":None
    }