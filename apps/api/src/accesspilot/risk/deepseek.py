"""DeepSeek 只读风险审查适配器。"""

import httpx

from accesspilot.agent.deepseek import HttpClient
from accesspilot.risk.review import (
    MalformedRiskReviewError,
    RiskReview,
    RiskReviewContext,
    validate_risk_review_json,
)

RISK_REVIEW_SYSTEM_PROMPT = """
你是 AccessPilot 的只读风险审查器。
你只能依据输入中的申请事实、目录风险等级、审批策略和 policies 做判断。
不得批准申请、不得声称权限已开通、不得引用 policies 之外的政策编号。
只输出 JSON，字段必须且只能是：
risk_level、outcome、summary、findings、citations。
risk_level 只能是 low/high/critical；
outcome 只能是 clear/requires_human_review/blocked；
citations 中每项只能包含 policy_code 和 reason。
""".strip()


class DeepSeekRiskReviewModel:
    """调用 DeepSeek，把只读上下文转换为严格 RiskReview。"""

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

    def review(self, context: RiskReviewContext) -> RiskReview:
        """生成严格审查；格式不合法时抛出可恢复的模型错误。"""

        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "messages": [
                    {"role": "system", "content": RISK_REVIEW_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": context.model_dump_json(),
                    },
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            timeout=30.0,
        )
        response.raise_for_status()

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise MalformedRiskReviewError(
                "DeepSeek 未返回合法的 RiskReview JSON"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise MalformedRiskReviewError("DeepSeek 返回了空风险审查")
        return validate_risk_review_json(content)
