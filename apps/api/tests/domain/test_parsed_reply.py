import pytest
from pydantic import ValidationError

from accesspilot.domain.models import ParsedReply


def test_parsed_reply_keeps_only_fields_explicitly_present_in_user_reply() -> None:
    """模型只提取用户这句话明确说出的申请时长，不擅自补全其余业务字段。"""

    reply = ParsedReply(duration_days=14)

    assert reply.duration_days == 14
    assert reply.employee_id is None
    assert reply.entitlement_id is None
    assert reply.justification is None
    assert reply.confirmed is None


def test_parsed_reply_rejects_unknown_fields() -> None:
    """模型不得伪造审批等未定义的业务事实。"""

    with pytest.raises(ValidationError) as error:
        ParsedReply(approved=True)  # type: ignore[call-arg]

    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_parsed_reply_requires_explicit_boolean_confirmation() -> None:
    """字符串 yes 不能被宽松转换成用户已经明确确认。"""

    with pytest.raises(ValidationError) as error:
        ParsedReply(confirmed="yes")  # type: ignore[arg-type]

    assert error.value.errors()[0]["type"] == "bool_type"
