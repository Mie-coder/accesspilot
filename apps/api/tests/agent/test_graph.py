from dataclasses import dataclass, field

from langgraph.checkpoint.memory import InMemorySaver

from accesspilot.agent.state import ConversationPhase, create_initial_state
from accesspilot.domain.models import RequestDraft


@dataclass
class FakeModel:
    """固定生成字段问题，避免真实模型让节点测试不稳定。"""

    questions: dict[str, str]
    requested_fields: list[str] = field(default_factory=list)

    def ask_for(self, field_name: str) -> str:
        self.requested_fields.append(field_name)
        return self.questions[field_name]


def test_ask_next_question_node_returns_one_question_without_mutation() -> None:
    # 初始草稿只有申请人；询问节点只能准备第一个缺失字段的追问。
    draft = RequestDraft(employee_id="EMP-001")
    initial_state = create_initial_state(draft)
    initial_snapshot = initial_state.copy()
    fake_model = FakeModel(
        questions={"entitlement_id": "请提供要申请的权限编号。"}
    )

    from accesspilot.agent.graph import ask_next_question_node

    update = ask_next_question_node(initial_state, fake_model)

    # 首次收集没有用户回复，只有下一条问题发生变化。
    assert update == {"next_question": "请提供要申请的权限编号。"}
    assert fake_model.requested_fields == ["entitlement_id"]

    # 询问节点只返回待合并的问题，不得改写输入状态或擅自确认提交。
    assert initial_state == initial_snapshot
    assert initial_state["next_question"] is None
    assert initial_state["phase"] is ConversationPhase.COLLECTING
    assert initial_state["draft"].confirmed is False


def test_apply_reply_and_ask_node_updates_one_field_and_returns_next_question() -> None:
    # 用户回答权限编号后，节点只补这一项并继续询问申请时长。
    draft = RequestDraft(employee_id="EMP-001")
    initial_state = create_initial_state(draft)
    initial_snapshot = initial_state.copy()
    fake_model = FakeModel(
        questions={"duration_days": "请提供申请时长（天）。"}
    )

    from accesspilot.agent.graph import apply_reply_and_ask_node

    update = apply_reply_and_ask_node(
        initial_state,
        user_reply="insighthub.customer_export",
        model=fake_model,
    )

    expected_draft = RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
    )
    # 只返回本轮需要合并的四项，不携带审批或开通状态。
    assert update == {
        "draft": expected_draft,
        "missing_fields": ["duration_days", "justification"],
        "phase": ConversationPhase.COLLECTING,
        "next_question": "请提供申请时长（天）。",
    }
    assert fake_model.requested_fields == ["duration_days"]

    # model_copy 生成新草稿，输入状态不得被原地修改或确认提交。
    assert initial_state == initial_snapshot
    assert initial_state["draft"] is draft
    assert initial_state["draft"].entitlement_id is None
    assert initial_state["draft"].confirmed is False
    assert update["draft"].confirmed is False


def test_initial_question_graph_runs_one_turn_and_ends_after_question() -> None:
    # 这张最小图只运行询问节点；生成问题后立即结束，不处理用户回答。
    draft = RequestDraft(employee_id="EMP-001")
    initial_state = create_initial_state(draft)
    fake_model = FakeModel(
        questions={"entitlement_id": "请提供要申请的权限编号。"}
    )

    from accesspilot.agent.graph import build_initial_question_graph

    graph = build_initial_question_graph(fake_model)
    result = graph.invoke(initial_state)

    assert result["draft"] is draft
    assert result["missing_fields"] == [
        "entitlement_id",
        "duration_days",
        "justification",
    ]
    assert result["phase"] is ConversationPhase.COLLECTING
    assert result["next_question"] == "请提供要申请的权限编号。"
    assert result["draft"].confirmed is False
    assert fake_model.requested_fields == ["entitlement_id"]


def test_checkpoint_restores_state_in_a_new_graph_instance() -> None:
    # 两张独立图只共享内存 Checkpointer，模拟程序重建后按 thread_id 恢复。
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "request-1"}}
    first_model = FakeModel(
        questions={"entitlement_id": "请提供要申请的权限编号。"}
    )

    from accesspilot.agent.graph import build_initial_question_graph

    first_graph = build_initial_question_graph(first_model, checkpointer=saver)
    first_graph.invoke(
        create_initial_state(RequestDraft(employee_id="EMP-001")),
        config=config,
    )

    # 新图不重新运行节点，只读取同一会话最后保存的状态。
    restarted_model = FakeModel(
        questions={"entitlement_id": "不应再次调用模型。"}
    )
    restarted_graph = build_initial_question_graph(
        restarted_model,
        checkpointer=saver,
    )
    restored = restarted_graph.get_state(config).values

    assert restored["draft"].employee_id == "EMP-001"
    assert restored["missing_fields"] == [
        "entitlement_id",
        "duration_days",
        "justification",
    ]
    assert restored["phase"] is ConversationPhase.COLLECTING
    assert restored["next_question"] == "请提供要申请的权限编号。"
    assert restored["draft"].confirmed is False
    assert first_model.requested_fields == ["entitlement_id"]
    assert restarted_model.requested_fields == []
