"""DeepSeek 结构化回复适配器。"""

from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from accesspilot.agent.structured_reply import MalformedStructuredOutputError
from accesspilot.domain.models import ParsedReply


class HttpResponse(Protocol):
    """适配器实际使用的最小 HTTP 响应边界。"""

    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]: ...


class HttpClient(Protocol):
    """允许测试注入假客户端，避免调用真实 DeepSeek。"""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> HttpResponse: ...


SYSTEM_PROMPT = """
你是 AccessPilot 的字段提取器。请把用户本轮明确提供的信息输出为 JSON。
只允许 employee_id、entitlement_id、duration_days、justification、confirmed。
没有提到的字段使用 null，不得猜测，不得输出批准或权限开通结果。
JSON 示例：{"duration_days": 14, "confirmed": null}
""".strip()


def validate_reply_json(model_text: str) -> ParsedReply:
    """把模型返回的 JSON 字符串校验成业务对象。"""

    try:
        return ParsedReply.model_validate_json(model_text)
    except ValidationError as error:
        raise MalformedStructuredOutputError(
            "DeepSeek 未返回合法的 ParsedReply JSON"
        ) from error


class DeepSeekStructuredReplyModel:
    """调用 DeepSeek，并把单轮回复转换成经过校验的 ParsedReply。"""

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str = "https://api.deepseek.com",
        client: HttpClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        """提取并校验本轮草稿增量；格式不合法时交给外层决定是否重试。"""

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if correction is not None:
            messages.append({"role": "system", "content": correction})
        messages.append({"role": "user", "content": user_reply})

        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            timeout=30.0,
        )
        response.raise_for_status()

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise MalformedStructuredOutputError(
                "DeepSeek 未返回合法的 ParsedReply JSON"
            ) from error

        if not isinstance(content, str) or not content.strip():
            raise MalformedStructuredOutputError("DeepSeek 返回了空内容")
        return validate_reply_json(content)
