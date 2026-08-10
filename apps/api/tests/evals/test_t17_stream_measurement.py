"""T17 流式观测合同红测。

这些测试固定可复现的观测边界，而不是给流式响应伪造一个目标值：

* ``SseLatencyRecorder`` 必须在任意字节分片、心跳和 CRLF 下识别安全事件；
* 首事件、首个非空回答增量和唯一终态的时间由 ASGI ``send`` 实际观察；
* 模型调用次数来自 Workspace 持久化事实，而不是 SSE 文本猜测。
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import (
    SqlAlchemyWorkspaceStore,
    hash_workspace_token,
)
from accesspilot.domain.models import ParsedReply
from accesspilot.evaluation import SseLatencyRecorder
from accesspilot.events import append_workspace_event
from accesspilot.main import create_app


def _sse_frame(
    event_type: str,
    *,
    turn_id: str = "turn-1",
    seq: int = 1,
    payload: dict[str, object] | None = None,
) -> bytes:
    envelope = {
        "schema_version": "v1",
        "turn_id": turn_id,
        "seq": seq,
        "occurred_at": "2026-08-11T00:00:00Z",
        "payload": payload or {},
    }
    return (
        f"id: {turn_id}:{seq}\r\n"
        f"event: {event_type}\r\n"
        f"data: {json.dumps(envelope, ensure_ascii=False)}\r\n"
        "\r\n"
    ).encode()


def _feed_frame_in_chunks(
    recorder: SseLatencyRecorder,
    frame: bytes,
    *,
    observed_ns: int,
) -> None:
    """把单个 SSE frame 切成不规则块，覆盖边界落在 CRLF/JSON 内。"""

    widths = (1, 4, 2, 9, 3, 17)
    offset = 0
    width_index = 0
    while offset < len(frame):
        width = widths[width_index % len(widths)]
        recorder.feed(
            frame[offset : offset + width],
            observed_ns=observed_ns,
        )
        offset += width
        width_index += 1


def _valid_stream_events() -> list[tuple[bytes, int]]:
    return [
        (b": heartbeat\r\n\r\n", 1_000_000_000),
        (
            _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
            1_002_000_000,
        ),
        (
            _sse_frame("message.delta", seq=2, payload={"text": ""}),
            1_003_000_000,
        ),
        (
            _sse_frame("message.delta", seq=3, payload={"text": "第一段"}),
            1_004_000_000,
        ),
        (
            _sse_frame(
                "message.completed",
                seq=4,
                payload={"turn_id": "turn-1", "message_id": "message-1", "content": "第一段"},
            ),
            1_006_000_000,
        ),
    ]


def test_latency_recorder_handles_arbitrary_chunks_heartbeat_and_crlf() -> None:
    recorder = SseLatencyRecorder(start_ns=1_000_000_000)

    for frame, observed_ns in _valid_stream_events():
        if frame.startswith(b": heartbeat"):
            for byte in (b":", b" heartbeat\r", b"\n\r", b"\n"):
                recorder.feed(byte, observed_ns=observed_ns)
        else:
            _feed_frame_in_chunks(recorder, frame, observed_ns=observed_ns)

    result = recorder.finish()

    assert result.first_event_ms == pytest.approx(2.0)
    assert result.first_token_ms == pytest.approx(4.0)
    assert result.completion_ms == pytest.approx(6.0)
    assert result.terminal_event == "message.completed"
    assert result.event_count == 4


def test_latency_recorder_marks_first_token_none_when_no_delta_exists() -> None:
    recorder = SseLatencyRecorder(start_ns=10_000_000_000)
    recorder.feed(
        _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
        observed_ns=10_001_000_000,
    )
    recorder.feed(
        _sse_frame(
            "message.completed",
            seq=2,
            payload={"turn_id": "turn-1", "message_id": "message-1", "content": "短回答"},
        ),
        observed_ns=10_002_000_000,
    )

    result = recorder.finish()

    assert result.first_event_ms == pytest.approx(1.0)
    assert result.first_token_ms is None
    assert result.completion_ms == pytest.approx(2.0)
    assert result.terminal_event == "message.completed"
    assert result.event_count == 2


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(
            [
                _sse_frame("message.delta", seq=1, payload={"text": "乱序"}),
                _sse_frame("turn.started", seq=2, payload={"turn_id": "turn-1"}),
                _sse_frame(
                    "message.completed",
                    seq=3,
                    payload={
                        "turn_id": "turn-1",
                        "message_id": "message-1",
                        "content": "乱序",
                    },
                ),
            ],
            id="event-order",
        ),
        pytest.param(
            [
                _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
                _sse_frame(
                    "message.completed",
                    seq=2,
                    payload={
                        "turn_id": "turn-1",
                        "message_id": "message-1",
                        "content": "完成",
                    },
                ),
                _sse_frame("message.delta", seq=3, payload={"text": "终态后"}),
            ],
            id="after-terminal",
        ),
        pytest.param(
            [
                _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
                _sse_frame(
                    "message.completed",
                    seq=2,
                    payload={
                        "turn_id": "turn-1",
                        "message_id": "message-1",
                        "content": "完成",
                    },
                ),
                _sse_frame(
                    "error.recoverable",
                    seq=3,
                    payload={"turn_id": "turn-1", "code": "SECOND_TERMINAL", "message": "重复终态"},
                ),
            ],
            id="duplicate-terminal",
        ),
        pytest.param(
            [
                _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
                _sse_frame("message.delta", seq=2, payload={"text": "未完成"}),
            ],
            id="missing-terminal",
        ),
    ],
)
def test_latency_recorder_fails_closed_for_order_or_terminal_contract(
    events: list[bytes],
) -> None:
    recorder = SseLatencyRecorder(start_ns=1_000_000_000)

    with pytest.raises(ValueError):
        for index, event in enumerate(events, start=1):
            recorder.feed(event, observed_ns=1_000_000_000 + index * 1_000_000)
        recorder.finish()


def test_latency_recorder_rejects_unknown_event_types() -> None:
    recorder = SseLatencyRecorder(start_ns=1_000_000_000)
    recorder.feed(
        _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
        observed_ns=1_001_000_000,
    )

    with pytest.raises(ValueError, match="event type"):
        recorder.feed(
            _sse_frame("unknown.internal", seq=2, payload={}),
            observed_ns=1_002_000_000,
        )


def test_latency_recorder_rejects_observed_time_going_backwards() -> None:
    recorder = SseLatencyRecorder(start_ns=1_000_000_000)
    recorder.feed(
        _sse_frame("turn.started", seq=1, payload={"turn_id": "turn-1"}),
        observed_ns=1_002_000_000,
    )

    with pytest.raises(ValueError, match="monotonic"):
        recorder.feed(
            _sse_frame("message.delta", seq=2, payload={"text": "倒退"}),
            observed_ns=1_001_000_000,
        )


class CompleteStructuredReplyModel:
    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        return ParsedReply(
            employee_id="EMP-001",
            entitlement_id="insighthub.customer_export",
            duration_days=14,
            justification="用于核验虚构客户数据",
            confirmed=False,
        )


class TwoDeltaAnswerStream:
    async def stream_answer(
        self,
        *,
        assistant_message: str,
        turn_id: str,
    ) -> AsyncIterator[str]:
        del assistant_message, turn_id
        yield "第一段"
        await asyncio.sleep(0)
        yield "第二段"


async def _stream_with_direct_asgi(
    app: Any,
    *,
    workspace_token: str,
    content: str,
    recorder: SseLatencyRecorder,
) -> int:
    body = json.dumps({"content": content}, ensure_ascii=False).encode()
    request_sent = False
    status_code = 0

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        # Keep the request open without reporting a disconnect. Starlette's
        # streaming response polls this receive callable while yielding frames.
        await asyncio.sleep(0.001)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if isinstance(chunk, bytes) and chunk:
                recorder.feed(chunk, observed_ns=time.perf_counter_ns())

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat/messages/stream",
            "raw_path": b"/api/chat/messages/stream",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cookie", f"accesspilot_workspace={workspace_token}".encode()),
            ],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    return status_code


def _model_calls(
    database_session_factory: sessionmaker[Session],
    workspace_token: str,
) -> int:
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
            )
        )
        assert workspace is not None
        return workspace.model_calls_used


def test_direct_asgi_stream_records_observed_latency_and_model_calls(
    database_session_factory: sessionmaker[Session],
    record_property: Any,
) -> None:
    with database_session_factory() as session:
        seed_catalog(session)
    app = create_app(
        store=SqlAlchemyWorkspaceStore(database_session_factory),
        settings=Settings(demo_mode_enabled=True),
        session_factory=database_session_factory,
        structured_reply_model=CompleteStructuredReplyModel(),
        answer_stream_model=TwoDeltaAnswerStream(),
    )
    client = TestClient(app)
    assert client.post("/api/workspaces").status_code == 201
    workspace_token = client.cookies.get("accesspilot_workspace")
    assert workspace_token is not None
    before_calls = _model_calls(database_session_factory, workspace_token)

    start_ns = time.perf_counter_ns()
    recorder = SseLatencyRecorder(start_ns=start_ns)
    status_code = asyncio.run(
        _stream_with_direct_asgi(
            app,
            workspace_token=workspace_token,
            content="申请客户数据导出 14 天，用于核验虚构客户数据",
            recorder=recorder,
        )
    )
    result = recorder.finish()
    after_calls = _model_calls(database_session_factory, workspace_token)
    model_calls = after_calls - before_calls

    record_property("first_event_ms", result.first_event_ms)
    record_property("first_token_ms", result.first_token_ms)
    record_property("completion_ms", result.completion_ms)
    record_property("terminal_event", result.terminal_event)
    record_property("model_calls", model_calls)
    record_property("adapter_mode", "deterministic_offline")

    assert status_code == 200
    assert result.first_event_ms is not None
    assert result.first_token_ms is not None
    assert result.completion_ms is not None
    assert 0 <= result.first_event_ms <= result.first_token_ms <= result.completion_ms
    assert result.terminal_event == "message.completed"
    assert result.event_count >= 4
    assert model_calls == 1


def _sse_ids(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"^id: (\d+)$", text, re.MULTILINE)]


def test_reconnect_observation_has_no_duplicate_persisted_event_ids(
    database_session_factory: sessionmaker[Session],
    record_property: Any,
) -> None:
    app = create_app(
        store=SqlAlchemyWorkspaceStore(database_session_factory),
        settings=Settings(demo_mode_enabled=True),
        session_factory=database_session_factory,
    )
    client = TestClient(app)
    assert client.post("/api/workspaces").status_code == 201
    workspace_token = client.cookies.get("accesspilot_workspace")
    assert workspace_token is not None
    with database_session_factory() as session:
        first = append_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type="message.user",
            payload={"content": "第一条"},
        )
        append_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type="message.assistant",
            payload={"content": "第二条"},
        )
        append_workspace_event(
            session,
            workspace_token=workspace_token,
            event_type="business.status",
            payload={"status": "answered"},
        )

    history = client.get("/api/events?follow=false")
    history_ids = _sse_ids(history.text)
    assert history.status_code == 200
    assert len(history_ids) >= 3
    cursor = first.id

    replay = client.get(
        "/api/events?follow=false",
        headers={"Last-Event-ID": str(cursor)},
    )
    replay_ids = _sse_ids(replay.text)
    duplicate_count = sum(1 for event_id in replay_ids if event_id <= cursor)
    duplicate_count += len(replay_ids) - len(set(replay_ids))

    record_property("reconnect_replayed_events", len(replay_ids))
    record_property("reconnect_duplicate_count", duplicate_count)

    assert replay.status_code == 200
    assert replay_ids
    assert all(event_id > cursor for event_id in replay_ids)
    assert duplicate_count == 0
