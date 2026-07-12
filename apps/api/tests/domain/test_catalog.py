import accesspilot.domain.catalog as catalog


def test_customer_export_has_high_risk_dual_approval_policy() -> None:
    entitlement = catalog.ENTITLEMENTS["insighthub.customer_export"]

    assert entitlement.risk_level is catalog.RiskLevel.HIGH
    assert entitlement.approval_policy is catalog.ApprovalPolicy.MANAGER_AND_DATA_OWNER
    assert entitlement.self_service_allowed is True
    assert entitlement.max_duration_days == 30
    assert "product_operations" in entitlement.eligible_departments


def test_raw_customer_export_is_not_available_through_self_service() -> None:
    entitlement = catalog.ENTITLEMENTS["insighthub.raw_customer_export"]

    assert entitlement.risk_level is catalog.RiskLevel.CRITICAL
    assert entitlement.self_service_allowed is False
    assert entitlement.approval_policy is catalog.ApprovalPolicy.MANUAL_SECURITY


def test_employee_eligibility_blocks_out_of_scope_requests() -> None:
    assert catalog.is_eligible_to_request("EMP-001", "insighthub.customer_export")
    assert not catalog.is_eligible_to_request("EMP-001", "insighthub.raw_customer_export")
    assert not catalog.is_eligible_to_request("EMP-001", "opsdesk.production_operator")
    assert catalog.is_eligible_to_request("EMP-005", "opsdesk.production_operator")


def test_policy_catalog_contains_eight_access_governance_rules() -> None:
    policy_codes = {policy.code for policy in catalog.POLICIES}

    assert len(catalog.POLICIES) == 8
    assert {
        "POL-001",
        "POL-002",
        "POL-003",
        "POL-004",
        "POL-005",
        "POL-006",
        "POL-007",
        "POL-008",
    } <= policy_codes
