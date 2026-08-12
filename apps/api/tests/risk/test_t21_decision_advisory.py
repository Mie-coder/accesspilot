"""T21 bounded DeepSeek advisory adapter tests."""

from typing import Any

import pytest

from accesspilot.decision_packets import (
    AdvisoryPolicyEvidence,
    DecisionAdvisoryContext,
    MalformedDecisionAdvisoryError,
)
from accesspilot.risk.decision_packet import DeepSeekDecisionAdvisoryModel


class FakeResponse:
    def __init__(self, content: object) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


class RecordingHttpClient:
    def __init__(self, content: object) -> None:
        self.response = FakeResponse(content)
        self.last_json: dict[str, Any] | None = None
        self.last_timeout: float | None = None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        del url, headers
        self.last_json = json
        self.last_timeout = timeout
        return self.response


def advisory_context() -> DecisionAdvisoryContext:
    return DecisionAdvisoryContext(
        duration_days=14,
        justification="已脱敏的业务理由",
        risk_level="high",
        max_duration_days=30,
        approval_policy="manager_and_data_owner",
        policies=[
            AdvisoryPolicyEvidence(
                policy_code="POL-003",
                title="高风险权限双审批",
                content="高风险权限需要两级人工审批。",
                version="v1",
                source="fictional_access_policy",
            )
        ],
    )


def test_deepseek_decision_advisory_uses_strict_schema_and_configured_timeout() -> None:
    client = RecordingHttpClient(
        """{
          "assessment": "risk",
          "summary": "需要人工核对最小期限。",
          "unknowns": ["用途证据待核对"],
          "recommendations": ["核对申请说明"],
          "citations": ["POL-003"]
        }"""
    )
    model = DeepSeekDecisionAdvisoryModel(
        api_key="test-key",
        model_name="deepseek-v4-flash",
        timeout_seconds=7.5,
        client=client,
    )

    advisory = model.review(advisory_context())

    assert advisory.assessment == "risk"
    assert client.last_timeout == 7.5
    assert client.last_json is not None
    assert client.last_json["response_format"] == {"type": "json_object"}
    outbound = client.last_json["messages"][-1]["content"]
    assert "request_id" not in outbound
    assert "employee_id" not in outbound
    assert "entitlement_code" not in outbound
    assert "approver_id" not in outbound


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        '{"approved": true}',
        (
            '{"assessment":"clear","summary":"x","unknowns":[],'
            '"recommendations":[],"citations":[],'
            '"approver_id":"EMP-002"}'
        ),
    ],
)
def test_deepseek_decision_advisory_rejects_malformed_or_extra_fields(
    content: object,
) -> None:
    model = DeepSeekDecisionAdvisoryModel(
        api_key="test-key",
        model_name="deepseek-v4-flash",
        client=RecordingHttpClient(content),
    )

    with pytest.raises(MalformedDecisionAdvisoryError):
        model.review(advisory_context())
