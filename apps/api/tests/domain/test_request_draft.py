from datetime import date

import accesspilot.domain.models as models

# 这个测试表达的业务规则是：
# 用户已经提供了系统和权限；
# 另外五项信息还没有提供；
# Agent 必须由程序确定缺失字段，而不是让大模型自由猜测


def test_incomplete_request_reports_missing_fields() -> None:
    draft = models.RequestDraft(
        system_name="InsightHub",
        entitlement_name="客户数据导出",
    )

    assert draft.missing_fields() == [
        "project_code",
        "data_scope",
        "business_reason",
        "start_date",
        "duration_days",
    ]


def test_complete_request_has_no_missing_fields() -> None:
    draft = models.RequestDraft(
        system_name="InsightHub",
        entitlement_name="客户数据导出",
        project_code="PRJ-AURORA",
        data_scope="华南区脱敏客户数据",
        business_reason="核验项目运营数据",
        start_date=date(2026, 7, 15),
        duration_days=14,
    )

    assert draft.missing_fields() == []
