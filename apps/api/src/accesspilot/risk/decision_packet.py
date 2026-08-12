"""DeepSeek adapter for the bounded Decision Packet advisory section."""

from typing import Literal

import httpx
from pydantic import ValidationError

from accesspilot.agent.deepseek import HttpClient
from accesspilot.decision_packets import (
    DecisionAdvisory,
    DecisionAdvisoryContext,
    MalformedDecisionAdvisoryError,
)

DECISION_ADVISORY_SYSTEM_PROMPT = """
你是 AccessPilot Decision Packet 的只读风险建议器。
输入只包含已脱敏申请事实与本轮政策证据。
你不得输出或选择申请人、权限编码、审批人、审批路线、批准/驳回动作、幂等键或开通工具调用。
只输出 JSON，字段必须且只能是：
assessment、summary、unknowns、recommendations、citations。
assessment 只能是 clear、risk 或 blocked。
citations 只能引用输入 policies 中已给出的 policy_code。
你的文本始终是 advisory，不得改变系统固定的人工审批路线。
""".strip()


class DeepSeekDecisionAdvisoryModel:
    """Call DeepSeek with de-identified facts and a strict output schema."""

    generation_mode: Literal["provider", "deterministic"] = "provider"

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 20.0,
        client: HttpClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.Client()

    def review(self, context: DecisionAdvisoryContext) -> DecisionAdvisory:
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "messages": [
                    {"role": "system", "content": DECISION_ADVISORY_SYSTEM_PROMPT},
                    {"role": "user", "content": context.model_dump_json()},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise MalformedDecisionAdvisoryError(
                    "DeepSeek 返回了空风险建议"
                )
            return DecisionAdvisory.model_validate_json(content)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as error:
            if isinstance(error, MalformedDecisionAdvisoryError):
                raise
            raise MalformedDecisionAdvisoryError(
                "DeepSeek 未返回合法的 DecisionAdvisory JSON"
            ) from error
