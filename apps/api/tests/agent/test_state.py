from accesspilot.agent.state import ConversationPhase
from accesspilot.domain.models import RequestDraft


def test_create_initial_state_uses_draft_and_conversation_defaults() -> None:
    # 新对话统一从草稿推导缺失字段，其余状态使用安全的初始值。
    draft = RequestDraft(employee_id="EMP-001")

    from accesspilot.agent.state import create_initial_state

    state = create_initial_state(draft)

    assert state["draft"] is draft
    assert state["missing_fields"] == [
        "entitlement_id",
        "duration_days",
        "justification",
    ]
    assert state["tool_summaries"] == []
    assert state["phase"] is ConversationPhase.COLLECTING
    assert state["recoverable_error"] is None
    # 新对话还没有经过收集节点，因此没有面向用户的下一条追问。
    assert state["next_question"] is None
