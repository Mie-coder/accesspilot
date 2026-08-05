from typing import Any
from uuid import UUID

import pytest

from accesspilot.rag.policies import PolicyMatch
from accesspilot.risk.deepseek import DeepSeekRiskReviewModel
from accesspilot.risk.review import MalformedRiskReviewError, RiskReviewContext


class FakeResponse:
    def __init__(self, content: object) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


class FakeHttpClient:
    def __init__(self, content: object) -> None:
        self.response = FakeResponse(content)
        self.last_json: dict[str, Any] | None = None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        self.last_json = json
        return self.response


def context() -> RiskReviewContext:
    return RiskReviewContext(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=14,
        justification="核验虚构项目运营数据",
        entitlement_risk_level="high",
        approval_policy="manager_and_data_owner",
        policies=[
            PolicyMatch(
                policy_code="POL-003",
                title="高风险权限双审批",
                content="高风险权限必须依次经过两级审批。",
                similarity=0.9,
            )
        ],
    )


def test_deepseek_risk_adapter_returns_strict_review() -> None:
    client = FakeHttpClient(
        """{
            "risk_level": "high",
            "outcome": "requires_human_review",
            "summary": "需要两级人工审批",
            "findings": ["申请期限为 14 天"],
            "citations": [
                {"policy_code": "POL-003", "reason": "要求双审批"}
            ]
        }"""
    )
    model = DeepSeekRiskReviewModel(
        api_key="test-key",
        model_name="deepseek-v4-flash",
        client=client,
    )

    review = model.review(context())

    assert review.risk_level == "high"
    assert review.citations[0].policy_code == "POL-003"
    assert client.last_json is not None
    assert client.last_json["response_format"] == {"type": "json_object"}
    assert "EMP-001" in client.last_json["messages"][-1]["content"]


@pytest.mark.parametrize("content", [None, "", '{"approved": true}'])
def test_deepseek_risk_adapter_rejects_malformed_review(content: object) -> None:
    model = DeepSeekRiskReviewModel(
        api_key="test-key",
        model_name="deepseek-v4-flash",
        client=FakeHttpClient(content),
    )

    with pytest.raises(MalformedRiskReviewError):
        model.review(context())
