import pytest

from accesspilot.agent.structured_reply import (
    MalformedStructuredOutputError,
    ReplyParsingFailed,
    parse_reply_with_retry,
)
from accesspilot.domain.models import ParsedReply


class SequencedReplyModel:
    """按顺序返回测试结果，模拟模型第一次格式错误、第二次纠正。"""

    def __init__(
        self,
        results: list[ParsedReply | MalformedStructuredOutputError],
    ) -> None:
        self.results = results
        self.corrections: list[str | None] = []

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        self.corrections.append(correction)
        result = self.results.pop(0)
        if isinstance(result, MalformedStructuredOutputError):
            raise result
        return result


def test_valid_structured_reply_does_not_retry() -> None:
    model = SequencedReplyModel([ParsedReply(duration_days=14)])

    result = parse_reply_with_retry("申请 14 天", model)

    assert result == ParsedReply(duration_days=14)
    assert model.corrections == [None]


def test_malformed_output_is_corrected_once() -> None:
    model = SequencedReplyModel(
        [
            MalformedStructuredOutputError("不是合法 JSON"),
            ParsedReply(duration_days=14),
        ]
    )

    result = parse_reply_with_retry("申请 14 天", model)

    assert result == ParsedReply(duration_days=14)
    assert model.corrections[0] is None
    assert model.corrections[1] is not None
    assert "只输出符合 Schema 的 JSON" in model.corrections[1]


def test_second_malformed_output_becomes_recoverable_error() -> None:
    model = SequencedReplyModel(
        [
            MalformedStructuredOutputError("第一次格式错误"),
            MalformedStructuredOutputError("第二次仍格式错误"),
        ]
    )

    with pytest.raises(ReplyParsingFailed):
        parse_reply_with_retry("申请 14 天", model)

    assert len(model.corrections) == 2
