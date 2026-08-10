"""权限名称实体解析的纯确定性合同。"""

from accesspilot.tools.catalog import (
    ENTITLEMENT_ALIAS_VERSION,
    EligibleAccessSummary,
    resolve_entitlement_candidates,
)


def eligible_candidates() -> list[EligibleAccessSummary]:
    return [
        EligibleAccessSummary(
            code="codeforge.repo_read",
            name="代码仓库只读",
            system_code="codeforge",
            system_name="代码协作平台",
            risk_level="low",
            max_duration_days=180,
            approval_policy="manager",
        ),
        EligibleAccessSummary(
            code="insighthub.customer_export",
            name="脱敏客户数据导出",
            system_code="insighthub",
            system_name="数据洞察中心",
            risk_level="high",
            max_duration_days=30,
            approval_policy="manager_and_data_owner",
        ),
        EligibleAccessSummary(
            code="insighthub.dashboard_view",
            name="InsightHub 仪表盘查看",
            system_code="insighthub",
            system_name="数据洞察中心",
            risk_level="low",
            max_duration_days=180,
            approval_policy="manager",
        ),
    ]


def test_resolution_matches_code_name_and_controlled_alias() -> None:
    assert ENTITLEMENT_ALIAS_VERSION == "v1"
    candidates = eligible_candidates()

    by_code = resolve_entitlement_candidates(candidates, "insighthub.dashboard_view")
    by_name = resolve_entitlement_candidates(candidates, "insighthub 仪表盘查看")
    by_alias = resolve_entitlement_candidates(candidates, "仪表盘查看")

    for result in (by_code, by_name, by_alias):
        assert result.status == "matched"
        assert result.target_field == "entitlement_id"
        assert [candidate.code for candidate in result.candidates] == [
            "insighthub.dashboard_view"
        ]


def test_system_name_can_return_typed_ambiguous_candidates_without_draft_mutation() -> None:
    result = resolve_entitlement_candidates(eligible_candidates(), "数据洞察中心")

    assert result.status == "ambiguous"
    assert result.target_field == "entitlement_id"
    assert [candidate.code for candidate in result.candidates] == [
        "insighthub.customer_export",
        "insighthub.dashboard_view",
    ]


def test_no_match_returns_current_eligible_access_and_never_fuzzy_matches() -> None:
    candidates = eligible_candidates()

    unknown = resolve_entitlement_candidates(candidates, "不存在的仪表盘")
    near_match = resolve_entitlement_candidates(candidates, "仪表盘查")

    for result in (unknown, near_match):
        assert result.status == "no_match"
        assert result.target_field == "entitlement_id"
        assert [candidate.code for candidate in result.eligible_access] == [
            "codeforge.repo_read",
            "insighthub.customer_export",
            "insighthub.dashboard_view",
        ]


def test_ineligible_or_non_self_service_permission_cannot_be_selected() -> None:
    candidates = eligible_candidates()

    result = resolve_entitlement_candidates(candidates, "原始客户数据导出")

    assert result.status == "no_match"
    assert result.candidates == []


def test_normalization_accepts_case_whitespace_and_common_separators_only() -> None:
    candidates = eligible_candidates()

    result = resolve_entitlement_candidates(candidates, "  INSIGHTHUB-CUSTOMER_EXPORT  ")

    assert result.status == "matched"
    assert [candidate.code for candidate in result.candidates] == [
        "insighthub.customer_export"
    ]
