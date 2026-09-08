"""The online provider classifies meaning before any application mutation."""

import pytest
from test_deepseek import FakeHttpClient, FakeResponse

from accesspilot.agent.deepseek import DeepSeekStructuredReplyModel
from accesspilot.agent.routing import IntentRoute
from accesspilot.agent.structured_reply import MalformedStructuredOutputError


def test_online_intent_uses_context_and_a_closed_output_schema() -> None:
    client = FakeHttpClient(FakeResponse('{"intent":"discover_eligible_access"}'))
    model = DeepSeekStructuredReplyModel("test-only", "test-model", client=client)
    bound = model.with_request_context({"expected_field": "entitlement_id"})
    assert bound.classify_intent("我不知道权限编号") == IntentRoute(
        intent="discover_eligible_access",
    )
    assert client.last_json is not None
    assert "entitlement_id" in str(client.last_json["messages"])
    assert "我不知道权限编号" in str(client.last_json["messages"])


@pytest.mark.parametrize("payload", [
    '{"intent":"grant_access"}', '{"intent":"request_access","confirmed":true}',
    '{"intent":"help","employee_id":"EMP-002"}', 'invalid',
])
def test_intent_cannot_carry_identity_confirmation_or_authority(payload: str) -> None:
    model = DeepSeekStructuredReplyModel(
        "test-only", "test-model", client=FakeHttpClient(FakeResponse(payload)),
    )
    with pytest.raises(MalformedStructuredOutputError):
        model.classify_intent("我想申请权限")


@pytest.mark.parametrize("text", [
    "用于整理政策说明", "用于核对申请状态", "我不想查询已有权限，我要申请代码仓库只读",
    "我想聊聊电影",
])
def test_ambiguous_fast_paths_defer_to_semantics(text: str) -> None:
    from accesspilot.agent.semantic_routing import fast_understanding

    assert fast_understanding(
        text, context={"expected_field": "justification", "eligible_permissions": []},
        safe_reason=True,
    ) is None


@pytest.mark.parametrize("text", ["我不知道权限编号", "那帮我查询可申请的权限"])
def test_no_key_fallback_does_not_guess_a_question_into_a_request(text: str) -> None:
    from accesspilot.agent.routing import route_message
    from accesspilot.agent.semantic_routing import offline_route

    assert offline_route(text, route_message(text)).intent == "unknown"


@pytest.mark.parametrize("text", ["我确认吗？", "确认提交是什么意思", "我确认？"])
def test_confirmation_questions_are_not_write_confirmation(text: str) -> None:
    from accesspilot.conversation import _explicit_confirmation_from_text

    assert _explicit_confirmation_from_text(text) is None
