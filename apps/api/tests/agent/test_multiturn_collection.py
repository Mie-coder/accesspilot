from dataclasses import dataclass, field

from accesspilot.agent.state import ConversationPhase, GraphState
from accesspilot.domain.models import RequestDraft


@dataclass
class FakeModel:
    """用固定回答代替真实模型，让多轮测试每次得到相同结果。"""

    questions: dict[str, str]
    requested_fields: list[str] = field(default_factory=list)

    def ask_for(self, field_name: str) -> str:
        self.requested_fields.append(field_name)
        return self.questions[field_name]


def test_partial_draft_asks_one_field_then_updates_state_for_next_turn() -> None:
    # 用户已说明申请人；第一轮只能继续询问固定顺序中的第一个缺失字段。
    draft = RequestDraft(employee_id="EMP-001")
    initial_state = GraphState(
        draft=draft,
        missing_fields=draft.missing_fields(),
        tool_summaries=[],
        phase=ConversationPhase.COLLECTING,
        recoverable_error=None,
    )
    fake_model = FakeModel(
        questions={
            "entitlement_id": "请提供要申请的权限编号。",
            "duration_days": "请提供申请时长（天）。",
        }
    )

    # 收集函数返回 tuple[GraphState, str | None]，每轮至多生成一个问题。
    from accesspilot.agent.collector import run_collection_turn

    first_state, first_question = run_collection_turn(
        initial_state,
        user_reply=None,
        model=fake_model,
    )

    assert first_state == initial_state
    assert first_question == "请提供要申请的权限编号。"

    # 用户回答只补入刚询问的字段，下一轮至多询问新的第一个缺失字段。
    second_state, second_question = run_collection_turn(
        first_state,
        user_reply="insighthub.customer_export",
        model=fake_model,
    )

    assert second_state["draft"].entitlement_id == "insighthub.customer_export"
    assert second_state["missing_fields"] == ["duration_days", "justification"]
    assert second_question == "请提供申请时长（天）。"
    assert fake_model.requested_fields == ["entitlement_id", "duration_days"]


def test_answering_last_missing_field_moves_to_awaiting_confirmation() -> None:
    # 草稿只差业务理由；回答后应停止追问并等待用户明确确认。
    draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=14,
    )
    initial_state = GraphState(
        draft=draft,
        missing_fields=draft.missing_fields(),
        tool_summaries=[],
        phase=ConversationPhase.COLLECTING,
        recoverable_error=None,
    )
    fake_model = FakeModel(questions={})

    from accesspilot.agent.collector import run_collection_turn

    updated_state, question = run_collection_turn(
        initial_state,
        user_reply="核验项目运营数据",
        model=fake_model,
    )

    assert updated_state["draft"].justification == "核验项目运营数据"
    assert updated_state["missing_fields"] == []
    assert updated_state["phase"] is ConversationPhase.AWAITING_CONFIRMATION
    assert question is None
    assert fake_model.requested_fields == []
