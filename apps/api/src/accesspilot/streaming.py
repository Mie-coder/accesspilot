"""当前轮 SSE 流式合同与断开安全收尾。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any, Protocol

from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from accesspilot.db.models import WorkspaceEventRecord


class AnswerStreamModel(Protocol):
    """把已安全生成的 assistant 文本转成真正的增量输出。"""

    def stream_answer(
        self,
        *,
        assistant_message: str,
        turn_id: str,
    ) -> AsyncIterator[str]:
        """每个 yield 都是上游的一个独立增量。"""


_DETERMINISTIC_CHUNK_SIZE = 12
_DETERMINISTIC_PUNCTUATION = frozenset("。！？!?；;，,、：:\n")


def split_deterministic_answer(answer: str) -> list[str]:
    """按安全 Unicode 长度分片，并在附近标点处优先结束。"""

    if len(answer) <= _DETERMINISTIC_CHUNK_SIZE:
        return [answer] if answer else []
    chunks: list[str] = []
    start = 0
    while start < len(answer):
        limit = min(start + _DETERMINISTIC_CHUNK_SIZE, len(answer))
        end = limit
        # 避免标点刚好位于窗口首位时产生单字符 chunk；仅在窗口
        # 后半段寻找边界，找不到就按固定 Unicode 长度切分。
        minimum_boundary = start + max(1, _DETERMINISTIC_CHUNK_SIZE // 2)
        for position in range(limit, minimum_boundary, -1):
            if answer[position - 1] in _DETERMINISTIC_PUNCTUATION:
                end = position
                break
        chunks.append(answer[start:end])
        start = end
    return chunks


class DeterministicAnswerStreamModel:
    """无外部 Key 时的透明离线回答流。"""

    async def stream_answer(
        self,
        *,
        assistant_message: str,
        turn_id: str,
    ) -> AsyncIterator[str]:
        del turn_id
        chunks = split_deterministic_answer(assistant_message)
        for index, chunk in enumerate(chunks):
            if index:
                # 仅让出事件循环，不制造“打字机”人为延迟。
                await asyncio.sleep(0)
            yield chunk


def _occurred_at(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(UTC)
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def encode_sse_frame(
    *,
    event_type: str,
    turn_id: str,
    seq: int,
    payload: dict[str, object],
    occurred_at: datetime | None = None,
) -> str:
    """编码当前轮统一 v1 envelope，数据库事件 id 不直接暴露为游标。"""

    envelope = {
        "schema_version": "v1",
        "turn_id": turn_id,
        "seq": seq,
        "occurred_at": _occurred_at(occurred_at),
        "payload": payload,
    }
    data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return f"id: {turn_id}:{seq}\nevent: {event_type}\ndata: {data}\n\n"


def encode_persisted_frame(
    event: WorkspaceEventRecord,
    *,
    turn_id: str,
    seq: int,
    payload_override: dict[str, object] | None = None,
) -> str:
    payload = dict(payload_override or event.payload)
    return encode_sse_frame(
        event_type=event.event_type,
        turn_id=turn_id,
        seq=seq,
        payload=payload,
        occurred_at=event.created_at,
    )


class SafeStreamingResponse(StreamingResponse):
    """在 ASGI 层捕获连接取消，给终态幂等收尾留下机会。"""

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        on_disconnect: Callable[[], Awaitable[None] | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._on_disconnect = on_disconnect

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except (asyncio.CancelledError, ClientDisconnect):
            if self._on_disconnect is not None:
                result = self._on_disconnect()
                if isawaitable(result):
                    await result
            raise
