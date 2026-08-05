from typing import Protocol

from accesspilot.domain.models import ParsedReply


class MalformedStructuredOutputError(ValueError):
    """模型输出无法按 ParsedReply Schema 解析。"""


class ReplyParsingFailed(RuntimeError):
    """模型经过一次纠正后仍未返回合法结构。"""


class StructuredReplyModel(Protocol):
    """从一轮用户回复提取结构化草稿增量的模型边界。"""

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        """返回合法 ParsedReply；格式错误时抛出明确异常。"""


CORRECTION_PROMPT = (
    "上一次输出无法通过校验。请根据同一条用户回复重新提取，"
    "只输出符合 Schema 的 JSON，不得猜测未提及字段。"
)


def parse_reply_with_retry(
    user_reply: str,
    model: StructuredReplyModel,
) -> ParsedReply:
    """解析用户回复；仅对结构化格式错误执行一次纠正重试。"""

    try:
        return model.parse_reply(user_reply)
    except MalformedStructuredOutputError:
        try:
            return model.parse_reply(
                user_reply,
                correction=CORRECTION_PROMPT,
            )
        except MalformedStructuredOutputError as error:
            # 两次格式错误后停止调用模型，交给图进入可恢复错误状态。
            raise ReplyParsingFailed("模型未能返回合法的结构化回复") from error
