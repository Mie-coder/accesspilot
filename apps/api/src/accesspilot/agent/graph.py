from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from accesspilot.agent.collector import QuestionModel, run_collection_turn
from accesspilot.agent.state import GraphState


def ask_next_question_node(
    state: GraphState,
    model: QuestionModel,
) -> dict[str, str | None]:
    """根据当前缺项生成下一句追问，不修改申请草稿。"""

    # 当前节点只负责提问；用户回答后的草稿更新由后续节点处理。
    _, question = run_collection_turn(state, user_reply=None, model=model)
    return {"next_question": question}


def apply_reply_and_ask_node(
    state: GraphState,
    user_reply: str,
    model: QuestionModel,
) -> dict[str, object]:
    """写入用户回答，并生成下一句追问。"""

    updated_state, question = run_collection_turn(
        state,
        user_reply=user_reply,
        model=model,
    )

    # 本轮用户回答改变了草稿、缺项和阶段；这些变更需交给图状态合并。
    return {
        "draft": updated_state["draft"],
        "missing_fields": updated_state["missing_fields"],
        "phase": updated_state["phase"],
        "next_question": question,
    }


def build_initial_question_graph(
    model: QuestionModel,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[GraphState, None, GraphState, GraphState]:
    """构建新申请第一轮的提问流程。"""

    graph = StateGraph(GraphState)

    # 图节点只接收 state；lambda 从外层带入运行时的 model。
    graph.add_node(
        "ask_question",
        lambda state: ask_next_question_node(state, model),
    )
    graph.add_edge(START, "ask_question")
    graph.add_edge("ask_question", END)

    return graph.compile(checkpointer=checkpointer)
