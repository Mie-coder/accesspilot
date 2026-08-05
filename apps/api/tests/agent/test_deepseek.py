from typing import Any

import pytest

from accesspilot.agent.deepseek import (
    DeepSeekStructuredReplyModel,
    validate_reply_json,
)
from accesspilot.agent.structured_reply import MalformedStructuredOutputError
from accesspilot.domain.models import ParsedReply


class FakeResponse:
    """模拟 DeepSeek 返回的一次成功响应。"""

    def __init__(self, content: object = '{"duration_days": 14}') -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "content": self.content,
                    }
                }
            ]
        }


class FakeHttpClient:
    """记录请求，测试时不访问真实网络。"""

    def __init__(self, response: FakeResponse | None = None) -> None:
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_json: dict[str, Any] | None = None
        self.response = response or FakeResponse()

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return self.response


def test_valid_json_becomes_parsed_reply() -> None:
    result = validate_reply_json('{"duration_days": 14}')

    assert result == ParsedReply(duration_days=14)


def test_invalid_json_becomes_retryable_model_error() -> None:
    with pytest.raises(MalformedStructuredOutputError):
        validate_reply_json('{"duration_days": 14, "approved": true}')


def test_deepseek_adapter_returns_validated_parsed_reply() -> None:
    client = FakeHttpClient()
    model = DeepSeekStructuredReplyModel(
        api_key="test-api-key",
        model_name="deepseek-v4-flash",
        client=client,
    )

    result = model.parse_reply("申请 14 天")

    assert result == ParsedReply(duration_days=14)
    assert client.last_url == "https://api.deepseek.com/chat/completions"
    assert client.last_headers == {
        "Authorization": "Bearer test-api-key",
        "Content-Type": "application/json",
    }
    assert client.last_json is not None
    assert client.last_json["model"] == "deepseek-v4-flash"
    assert client.last_json["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "content",
    ["", "not-json", None, '{"approved": true}'],
)
def test_deepseek_adapter_rejects_malformed_content(content: object) -> None:
    model = DeepSeekStructuredReplyModel(
        api_key="test-api-key",
        model_name="deepseek-v4-flash",
        client=FakeHttpClient(FakeResponse(content)),
    )

    with pytest.raises(MalformedStructuredOutputError):
        model.parse_reply("申请 14 天")


def test_deepseek_adapter_includes_correction_without_changing_user_reply() -> None:
    client = FakeHttpClient()
    model = DeepSeekStructuredReplyModel(
        api_key="test-api-key",
        model_name="deepseek-v4-flash",
        client=client,
    )

    model.parse_reply("申请 14 天", correction="请只输出 JSON")

    assert client.last_json is not None
    assert client.last_json["messages"][-2:] == [
        {"role": "system", "content": "请只输出 JSON"},
        {"role": "user", "content": "申请 14 天"},
    ]
