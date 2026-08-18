from typing import Protocol

from accesspilot.agent.state import CollectionGraphState, ConversationPhase


class QuestionModel(Protocol):
    """能根据当前缺失字段生成一句追问的模型接口。"""

    def ask_for(self, field_name: str) -> str:
        """返回针对该字段的一句追问。"""


def run_collection_turn(
    state: CollectionGraphState,
    user_reply: str | None,
    model: QuestionModel,
) -> tuple[CollectionGraphState, str | None]:
    """处理一轮收集：补一个字段，并最多提出下一个问题。"""

    # 第一轮尚无用户回复：保持草稿不变，只针对第一个缺失字段提问。
    if user_reply is None:
        missing_fields = state["missing_fields"]

        # 所有业务字段齐全时，不再生成追问，交给后续确认节点处理。
        if not missing_fields:
            return state, None

        first_missing_field = missing_fields[0]
        question = model.ask_for(first_missing_field)
        return state, question
    # 用户回答后，只补进上一轮正在追问的第一个缺失字段。
    first_missing_field = state["missing_fields"][0]
    updated_draft = state["draft"].model_copy(
        update={first_missing_field: user_reply}
    )

    # 草稿更新后重新计算仍缺的字段，供下一轮决定是否继续追问。
    updated_missing_fields = updated_draft.missing_fields()
    updated_state: CollectionGraphState = {
        **state,
        "draft": updated_draft,
        "missing_fields": updated_missing_fields,
    }

    # 字段完整时停止追问；确认提交由后续节点单独处理。
    if not updated_missing_fields:
        updated_state["phase"] = ConversationPhase.AWAITING_CONFIRMATION
        return updated_state, None

    next_missing_field = updated_missing_fields[0]
    next_question = model.ask_for(next_missing_field)
    return updated_state, next_question
