import pytest
from pydantic import ValidationError

from accesspilot.domain.models import RequestDraft


def test_incomplete_request_reports_business_fields_in_fixed_order() -> None:
    draft = RequestDraft(employee_id="EMP-001")

    assert draft.missing_fields() == [
        "entitlement_id",
        "duration_days",
        "justification",
    ]


def test_complete_unconfirmed_request_cannot_enter_approval() -> None:
    draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="ENT-CUSTOMER-EXPORT",
        duration_days=14,
        justification="核验项目运营数据",
    )

    assert draft.missing_fields() == []
    assert draft.confirmed is False
    assert draft.can_enter_approval() is False


def test_confirmed_valid_request_can_enter_approval() -> None:
    draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="ENT-CUSTOMER-EXPORT",
        duration_days=14,
        justification="  核验项目运营数据  ",
        confirmed=True,
    )

    assert draft.justification == "核验项目运营数据"
    assert draft.can_enter_approval() is True


def test_blank_justification_is_reported_as_missing() -> None:
    draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="ENT-CUSTOMER-EXPORT",
        duration_days=14,
        justification="   ",
        confirmed=True,
    )

    assert draft.missing_fields() == ["justification"]
    assert draft.can_enter_approval() is False


def test_session_and_derived_state_are_not_draft_fields() -> None:
    assert set(RequestDraft.model_fields) == {
        "employee_id",
        "entitlement_id",
        "duration_days",
        "justification",
        "confirmed",
    }

    with pytest.raises(ValidationError) as error:
        RequestDraft(workspace="workspace-from-client")  # type: ignore[call-arg]

    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("duration_days", [0, -1])
def test_non_positive_duration_is_invalid(duration_days: int) -> None:
    with pytest.raises(ValidationError) as error:
        RequestDraft(duration_days=duration_days)

    assert error.value.errors()[0]["type"] == "duration_not_positive"
    assert error.value.errors()[0]["msg"] == "申请期限必须是正整数"
