"""多意图对话路由：安全标记可与一个业务意图并存。"""

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, StrictBool

ConversationIntent = Literal[
    "discover_eligible_access",
    "list_active_access",
    "policy_question",
    "request_access",
    "request_status",
    "security_probe",
    "help",
]


class IntentRoute(BaseModel):
    """一轮只允许一个业务意图，安全探测使用独立标记。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: ConversationIntent
    security_probe: StrictBool = False


class IntentRouter(Protocol):
    """意图路由器边界，输出必须通过 IntentRoute 校验。"""

    def route(self, content: str) -> object: ...


class IntentRoutingFailed(RuntimeError):
    """路由器输出不符合严格意图 Schema。"""


SECURITY_MARKERS = (
    "system prompt",
    "system promote",
    "系统promote",
    "系统prompt",
    "系统 prompt",
    "系统提示词",
    "api key",
    "apikey",
    "api_key",
    "密钥",
    "隐藏推理",
    "chain of thought",
    "开发者消息",
    "绕过权限",
)

EXPLICIT_HELP_MARKERS = (
    "你能做什么",
    "你可以做什么",
    "可以做什么",
    "怎么使用",
    "如何使用",
    "使用帮助",
)


def _contains_any(content: str, markers: tuple[str, ...]) -> bool:
    return any(marker in content for marker in markers)


def is_explicit_help_query(content: str) -> bool:
    """区分真正的帮助问题与申请收集中的简短自由文本。"""

    normalized = " ".join(content.casefold().split())
    return normalized.strip("？?！!。.,， ") == "帮助" or _contains_any(
        normalized,
        EXPLICIT_HELP_MARKERS,
    )


def route_message(content: str) -> IntentRoute:
    """用确定性高精度规则路由核心业务问法，不额外消耗模型配额。"""

    normalized = " ".join(content.casefold().split())
    security_probe = _contains_any(normalized, SECURITY_MARKERS)

    if _contains_any(
        normalized,
        ("申请状态", "申请进度", "审批状态", "审批进度", "申请到哪"),
    ):
        intent: ConversationIntent = "request_status"
    elif _contains_any(
        normalized,
        ("现在有什么权限", "已有权限", "已经有什么权限", "拥有的权限", "有效授权"),
    ):
        intent = "list_active_access"
    elif _contains_any(
        normalized,
        (
            "能申请什么",
            "可以申请什么",
            "还能申请什么",
            "可申请权限",
            "还能访问什么权限",
            "可以访问什么权限",
        ),
    ):
        intent = "discover_eligible_access"
    elif _contains_any(
        normalized,
        ("政策", "规定", "自己审批", "自审批", "权限原则"),
    ):
        intent = "policy_question"
    elif is_explicit_help_query(normalized):
        intent = "help"
    elif (
        _contains_any(
            normalized,
            (
                "申请",
                "确认提交",
                "确认申请",
                "我确认",
                "不确认",
                "暂不确认",
                "不要提交",
                "用于",
                "为了",
            ),
        )
        or re.search(r"\d+\s*天", normalized) is not None
        or re.search(r"\bemp-\d+\b", normalized) is not None
        or re.search(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*\b", normalized) is not None
    ):
        intent = "request_access"
    elif security_probe:
        intent = "security_probe"
    else:
        intent = "help"

    return IntentRoute(intent=intent, security_probe=security_probe)


def route_with_validation(content: str, router: IntentRouter) -> IntentRoute:
    """校验路由输出；不合法时失败闭合，不猜测业务意图。"""

    try:
        return IntentRoute.model_validate(router.route(content))
    except Exception as error:
        raise IntentRoutingFailed("意图路由结果不可用") from error


class DeterministicIntentRouter:
    """离线与默认路由器，输出可解释且不增加模型调用。"""

    def route(self, content: str) -> IntentRoute:
        return route_message(content)
