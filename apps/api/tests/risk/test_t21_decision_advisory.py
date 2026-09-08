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


def test_advisory_receives_verified_presence_without_identity_values() -> None:
    from accesspilot.db.models import AccessRequestRecord, EntitlementRecord
    from accesspilot.decision_packets import _advisory_context
    from accesspilot.rag.policies import PolicyMatch

    context = _advisory_context(
        AccessRequestRecord(requester_id="EMP-001", entitlement_code="insighthub.dashboard_view",
                            duration_days=7, justification="用于给客户演示用的"),
        EntitlementRecord(code="insighthub.dashboard_view", risk_level="low",
                          max_duration_days=180, approval_policy="manager"),
        [PolicyMatch(policy_code="POL-001", title="申请字段完整性", content="字段完整性要求",
                     version="v1", source="fictional_access_policy", similarity=1.0)],
    )
    assert context.requester_verified is True
    assert context.entitlement_verified is True
    outbound = context.model_dump_json()
    assert "EMP-001" not in outbound and "insighthub.dashboard_view" not in outbound
    from accesspilot.risk.decision_packet import DECISION_ADVISORY_SYSTEM_PROMPT
    assert "脱敏省略不代表业务缺失" in DECISION_ADVISORY_SYSTEM_PROMPT


@pytest.mark.parametrize("claim", ["缺失身份和具体权限", "申请人身份未提供，无法核实完整性"])
def test_verified_fields_cannot_be_relabelled_as_missing(claim: str) -> None:
    from accesspilot.decision_packets import DecisionAdvisory, _advisory_is_safe

    advisory = DecisionAdvisory(assessment="blocked", summary=claim, unknowns=[],
                                recommendations=[], citations=[])
    assert not _advisory_is_safe(advisory, entitlement_code="insighthub.dashboard_view")


def test_new_advisory_prompt_requires_a_supported_conclusion_before_analysis() -> None:
    from accesspilot.risk.decision_packet import DECISION_ADVISORY_SYSTEM_PROMPT

    assert "summary 必须先以" in DECISION_ADVISORY_SYSTEM_PROMPT
    for conclusion in ("建议通过", "建议补充材料后再审", "建议不通过", "暂无法给出可靠建议"):
        assert conclusion in DECISION_ADVISORY_SYSTEM_PROMPT
    assert "不得为给出明确结论而猜测" in DECISION_ADVISORY_SYSTEM_PROMPT
