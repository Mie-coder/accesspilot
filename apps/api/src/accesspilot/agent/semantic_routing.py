"""Semantic routing behind deterministic safety and explicit control boundaries."""

import re
from dataclasses import dataclass
from typing import Literal

import httpx

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.routing import IntentRoute, is_explicit_help_query, route_message
from accesspilot.agent.safety import redact_sensitive_content
from accesspilot.agent.structured_reply import MalformedStructuredOutputError
from accesspilot.domain.models import ParsedReply


def control_route(content: str) -> IntentRoute | None:
    """Only safety, numbers and explicit controls bypass online understanding."""

    normalized = content.casefold().strip().strip("。！! ")
    route = route_message(content)
    if route.security_probe or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", normalized):
        return route
    if normalized in {"确认提交", "确认申请", "我确认", "不确认", "暂不确认", "不要提交"}:
        return IntentRoute(intent="request_access")
    if normalized in {"取消", "取消申请", "算了", "不申请了", "先不申请", "先不申请了"}:
        return IntentRoute(intent="help")
    return None


def understand_intent(
    model: DeepSeekStructuredReplyModel, content: str,
) -> tuple[IntentRoute, Literal["parsed", "unavailable"]]:
    """Unreliable provider output never falls through to the request parser."""

    try:
        return model.classify_intent(redact_sensitive_content(content)), "parsed"
    except (httpx.HTTPError, TimeoutError, MalformedStructuredOutputError):
        return IntentRoute(intent="unknown"), "unavailable"


@dataclass(frozen=True)
class FastUnderstanding:
    route: IntentRoute
    reply: ParsedReply | None = None


def fast_understanding(
    content: str, *, context: dict[str, object], safe_reason: bool,
) -> FastUnderstanding | None:
    """Conservative zero-provider paths; broad application keywords never win."""

    controlled = control_route(content)
    if controlled is not None:
        return FastUnderstanding(
            controlled, None if controlled.security_probe else ParsedReply(),
        )
    route = route_message(content)
    normalized = content.strip().strip("？?！!。 ")
    single_query = (
        not re.search(r"不|别|取消|，|,|；|;|。", normalized)
        and not normalized.startswith(("用于", "为了", "因为"))
        and (
            re.search(r"什么|哪些|如何|怎么|是否|能否|吗$", normalized) is not None
            or re.match(r"^(?:请|那|再|先|帮我|麻烦|我想)*(?:查询|查看|看看|列出)", normalized)
            is not None
        )
    )
    if single_query and route.intent in {
        "discover_eligible_access", "list_active_access", "request_status", "policy_question",
    }:
        return FastUnderstanding(route)
    if is_explicit_help_query(content) or content.strip() in {"请帮助", "help", "功能"}:
        return FastUnderstanding(IntentRoute(intent="help"))
    permissions = context.get("eligible_permissions")
    entries = permissions if isinstance(permissions, list) else []
    for permission in entries:
        if isinstance(permission, dict) and content.strip() in {
            permission.get("code"), permission.get("name"),
        }:
            return FastUnderstanding(
                IntentRoute(intent="request_access"),
                ParsedReply(entitlement_id=permission["code"]),
            )
    if context.get("expected_field") == "duration_days":
        duration = re.fullmatch(r"([1-9][0-9]{0,3})\s*天[。！! ]*", content.strip())
        if duration:
            return FastUnderstanding(
                IntentRoute(intent="request_access"), ParsedReply(duration_days=int(duration[1])),
            )
    # Free prose is a safe field answer only after excluding questions,
    # selection/change signals and catalog references. Ambiguity goes to LLM.
    if context.get("expected_field") == "justification" and safe_reason:
        refers_to_catalog = any(
            isinstance(entry, dict) and any(
                isinstance(entry.get(key), str) and entry[key] in content
                for key in ("code", "name", "system_name")
            ) for entry in entries
        )
        plain_purpose = re.fullmatch(
            r"(?:因为|用于|为了|给).{1,40}|.{1,20}(?:需要|汇报|培训|演示|核对|排查|联调)",
            normalized,
        )
        if plain_purpose and not refers_to_catalog and not re.search(
            r"查询|查一下|查查|看看|了解|不知道|想知道|帮我|权限|政策|状态|换|改成|申请|取消|提交",
            content,
        ):
            return FastUnderstanding(
                IntentRoute(intent="request_access"), ParsedReply(justification=content),
            )
    return None


def offline_route(content: str, route: IntentRoute) -> IntentRoute:
    """The no-key demo cannot guess unfamiliar questions into application writes."""

    if (
        route.security_probe or control_route(content) is not None
        or is_explicit_help_query(content)
    ):
        return route
    if (route.intent == "help" and re.search(r"不知道|不清楚|[？?]", content)) or (
        route.intent == "request_access"
        and re.search(r"查询|不知道|不清楚|什么|哪些|怎么|如何|[？?]|吗[。!！]*$", content)
    ):
        return IntentRoute(intent="unknown")
    return route
