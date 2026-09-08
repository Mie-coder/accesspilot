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
requester_verified 和 entitlement_verified 为 true 表示后端已核验申请人及具体权限；
姓名、员工编号和权限编码因最小信息原则不传入。脱敏省略不代表业务缺失，
不得将已核验字段说成缺失或要求重复提供身份。业务理由仅为申请人陈述，真实性仍待人工核对。
policies 是检索参考，命中不等于适用于本次申请；根据 risk_level、approval_policy 和条款范围解释，
不得把高风险双审批套用于 low/manager 的申请，也不得推测此申请属于客户数据导出。
你不得输出或选择申请人、权限编码、审批人、审批路线、批准/驳回动作、幂等键或开通工具调用。
只输出 JSON，字段必须且只能是：
assessment、summary、unknowns、recommendations、citations。
assessment 只能是 clear、risk 或 blocked。
citations 只能引用输入 policies 中已给出的 policy_code。
summary 必须先以“建议通过”“建议补充材料后再审”“建议不通过”或“暂无法给出可靠建议”之一开头，
结论后用句号，再给本次申请的具体依据，不重复堆砌 unknowns 和 recommendations。
只有没有待补材料、没有尚待解决的风险且事实支持时才建议通过，
此时 assessment 为 clear、unknowns 为空；
固定的人工审批和常规用途核对本身不等于申请缺材料，也不能说用途真实性已验证。
确有影响判断的材料缺口时建议补充材料后再审，并在 unknowns 列明具体缺什么、
recommendations 说明补什么。
有明确事实和适用政策支持不通过时才建议不通过；仅 risk/blocked 标签不足以作此结论。
事实不足、依据冲突或不能判断时，使用暂无法给出可靠建议，说明具体原因；不得为给出明确结论而猜测。
你的文本始终是 advisory，不得改变系统固定的人工审批路线，不得声称已经批准或开通。
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
