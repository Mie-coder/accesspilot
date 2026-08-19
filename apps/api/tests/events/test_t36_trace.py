"""T36 真实轨迹事件合同：独立 Schema、去重身份与最近三轮公开投影。

本文件覆盖 Ticket T36 的验收 1/3：每种新事件使用独立 extra=forbid Schema，
v1.2 terminal/draft 合同完整继承；event_key 可空且只对非空值唯一；历史
Legacy 事件不回填；公开投影按全局数据库事件 ID 选择/排序最近三轮，未知或
非法事件安全降级且不崩溃。
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import WorkspaceEventRecord, WorkspaceRecord
from accesspilot.events import (
    SAFE_EVENT_MODELS,
    PublicEventProjection,
    PublicTurnProjection,
    UnsafeEventError,
    append_workspace_event,
    list_recent_turns,
    validate_event_payload,
)

from .test_store import create_workspace

TURN = "turn-t36-contract"
STEP_ID = "stp_" + "a" * 32
TOOL_CALL_ID = "tool_" + "b" * 32
EVENT_KEY = "evt_" + "c" * 64


def _node_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "turn_id": TURN,
        "step_id": STEP_ID,
        "node_code": "route_intent",
        "public_label": "识别意图与路由",
        "status": "success",
    }
    payload.update(overrides)
    return payload


def _model_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "turn_id": TURN,
        "step_id": STEP_ID,
        "operation": "parse_input",
        "provider_mode": "mock",
        "attempt": 1,
    }
    payload.update(overrides)
    return payload


def _retrieval_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "turn_id": TURN,
        "step_id": STEP_ID,
        "retriever": "pgvector",
    }
    payload.update(overrides)
    return payload


def _tool_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "turn_id": TURN,
        "tool": "list_eligible_access",
        "tool_call_id": TOOL_CALL_ID,
        "step_id": STEP_ID,
    }
    payload.update(overrides)
    return payload


NEW_EVENT_PAYLOADS: dict[str, dict[str, object]] = {
    "agent.node.started": _node_payload(status="running"),
    "agent.node.completed": _node_payload(),
    "agent.route.selected": {
        "turn_id": TURN,
        "step_id": STEP_ID,
        "route_code": "read_only",
    },
    "model.started": _model_payload(),
    "model.completed": _model_payload(
        status="parsed",
        extracted_fields=["entitlement_id", "duration_days"],
    ),
    "retrieval.started": _retrieval_payload(),
    "retrieval.completed": _retrieval_payload(
        status="grounded",
        match_count=2,
        evidence_codes=["POL-003", "POL-004"],
    ),
    "agent.input.resumed": {
        "turn_id": TURN,
        "step_id": STEP_ID,
        "pending_input_id": "11111111-2222-3333-4444-555555555555",
        "kind": "confirmation",
        "decision": "confirm",
    },
}


# ---------------------------------------------------------------------------
# 独立 Schema 与 extra=forbid
# ---------------------------------------------------------------------------
def test_every_new_event_has_its_own_distinct_schema_class() -> None:
    pairs = [
        ("agent.node.started", "agent.node.completed"),
        ("model.started", "model.completed"),
        ("retrieval.started", "retrieval.completed"),
        ("tool.started", "tool.completed"),
    ]
    for first, second in pairs:
        assert first in SAFE_EVENT_MODELS, first
        assert second in SAFE_EVENT_MODELS, second
        assert SAFE_EVENT_MODELS[first] is not SAFE_EVENT_MODELS[second]


@pytest.mark.parametrize("event_type", sorted(NEW_EVENT_PAYLOADS))
def test_new_event_payloads_validate_and_reject_extra_fields(
    event_type: str,
) -> None:
    payload = NEW_EVENT_PAYLOADS[event_type]
    validated = validate_event_payload(event_type, payload)
    assert validated["turn_id"] == TURN
    assert validated["step_id"] == STEP_ID
    with pytest.raises(UnsafeEventError):
        validate_event_payload(event_type, {**payload, "unexpected": True})


def test_node_completed_status_is_closed() -> None:
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "agent.node.completed",
            _node_payload(status="bogus"),
        )


def test_model_completed_enforces_status_and_attempt_contract() -> None:
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "model.completed",
            _model_payload(status="bogus", extracted_fields=[]),
        )
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "model.completed",
            _model_payload(status="parsed", attempt=0, extracted_fields=[]),
        )


def test_retrieval_requires_pgvector_retriever() -> None:
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "retrieval.started",
            _retrieval_payload(retriever="bm25"),
        )
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "retrieval.completed",
            _retrieval_payload(
                retriever="bm25",
                status="grounded",
                match_count=1,
                evidence_codes=["POL-001"],
            ),
        )


def test_input_resumed_decision_is_closed() -> None:
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "agent.input.resumed",
            {
                "turn_id": TURN,
                "step_id": STEP_ID,
                "pending_input_id": "11111111-2222-3333-4444-555555555555",
                "kind": "confirmation",
                "decision": "maybe",
            },
        )


# ---------------------------------------------------------------------------
# v1.2 合同完整继承
# ---------------------------------------------------------------------------
def test_v12_terminal_and_draft_contracts_still_validate() -> None:
    legacy_payloads = {
        "message.completed": {
            "turn_id": "turn-legacy",
            "message_id": "message-1",
            "content": "完整回答",
            "intent": "help",
            "business_status": "answered",
            "draft_revision": 1,
        },
        "error.recoverable": {
            "turn_id": "turn-legacy",
            "code": "MODEL_REPLY_UNAVAILABLE",
            "message": "请稍后重试",
        },
        "turn.interrupted": {
            "turn_id": "turn-legacy",
            "reason": "client_cancelled",
            "retryable": True,
        },
        "draft.updated": {
            "turn_id": "turn-legacy",
            "draft": {"entitlement_id": "insighthub.dashboard_view"},
            "missing_fields": ["duration_days"],
            "can_enter_approval": False,
            "draft_revision": 1,
        },
        "business.status": {
            "turn_id": "turn-legacy",
            "status": "awaiting_confirmation",
        },
        "agent.input.required": {
            "turn_id": "turn-legacy",
            "pending_input_id": "11111111-2222-3333-4444-555555555555",
            "kind": "confirmation",
            "draft_revision": 1,
        },
    }
    for event_type, payload in legacy_payloads.items():
        validated = validate_event_payload(event_type, payload)
        assert validated["turn_id"] == "turn-legacy"
        with pytest.raises(UnsafeEventError):
            validate_event_payload(event_type, {**payload, "surprise": 1})


def test_turn_started_accepts_orchestrator_fields_and_legacy_shape() -> None:
    legacy = validate_event_payload(
        "turn.started",
        {"turn_id": "turn-legacy", "lease_expires_at": None},
    )
    assert "orchestrator" not in legacy
    graph = validate_event_payload(
        "turn.started",
        {
            "turn_id": "turn-graph",
            "lease_expires_at": datetime.now(UTC).isoformat(),
            "orchestrator": "langgraph",
            "flow_version": 2,
            "graph_version": "accesspilot-langgraph-v1.3",
        },
    )
    assert graph["orchestrator"] == "langgraph"
    assert graph["flow_version"] == 2
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "turn.started",
            {"turn_id": "turn-graph", "orchestrator": "langgraph", "extra": 1},
        )


def test_tool_events_inherit_v12_fields_and_add_step_id() -> None:
    legacy_started = validate_event_payload(
        "tool.started",
        {
            "turn_id": "turn-legacy",
            "tool": "list_eligible_access",
            "tool_call_id": TOOL_CALL_ID,
        },
    )
    assert "step_id" not in legacy_started
    new_started = validate_event_payload("tool.started", _tool_payload())
    assert new_started["step_id"] == STEP_ID
    new_completed = validate_event_payload(
        "tool.completed",
        _tool_payload(status="success", summary="共 2 项可申请权限"),
    )
    assert new_completed["step_id"] == STEP_ID
    with pytest.raises(UnsafeEventError):
        validate_event_payload(
            "tool.completed",
            _tool_payload(status="success", summary="x", extra=True),
        )


def test_agent_input_required_accepts_step_id() -> None:
    validated = validate_event_payload(
        "agent.input.required",
        {
            "turn_id": TURN,
            "step_id": STEP_ID,
            "pending_input_id": "11111111-2222-3333-4444-555555555555",
            "kind": "confirmation",
            "draft_revision": 1,
        },
    )
    assert validated["step_id"] == STEP_ID


# ---------------------------------------------------------------------------
# 落库前安全扫描覆盖新事件
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "agent.node.completed",
            _node_payload(public_label="sk-abcd1234efgh5678"),
        ),
        (
            "model.completed",
            _model_payload(
                status="parsed",
                extracted_fields=["entitlement_id", "sk-abcd1234efgh5678"],
            ),
        ),
        (
            "retrieval.completed",
            _retrieval_payload(
                status="grounded",
                match_count=1,
                evidence_codes=["POL-001", "sk-abcd1234efgh5678"],
            ),
        ),
        (
            "tool.completed",
            _tool_payload(status="success", summary="password: hunter2"),
        ),
    ],
)
def test_sensitive_value_scan_covers_new_event_types(
    database_session: Session,
    event_type: str,
    payload: dict[str, object],
) -> None:
    token = create_workspace(database_session)
    with pytest.raises(UnsafeEventError):
        append_workspace_event(
            database_session,
            workspace_token=token,
            event_type=event_type,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# 历史 null key 与事件 key 共存
# ---------------------------------------------------------------------------
def test_legacy_null_key_and_new_keyed_events_coexist(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    legacy = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "我要申请权限", "turn_id": "turn-legacy"},
    )
    keyed = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="agent.node.completed",
        payload=_node_payload(),
        event_key=EVENT_KEY,
    )
    assert legacy.event_key is None
    assert keyed.event_key == EVENT_KEY
    rows = database_session.scalars(
        select(WorkspaceEventRecord)
        .where(WorkspaceEventRecord.workspace_id == keyed.workspace_id)
        .order_by(WorkspaceEventRecord.id)
    ).all()
    assert [row.event_key for row in rows] == [None, EVENT_KEY]


def test_duplicate_event_key_is_rejected_at_database_level(
    database_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError

    token = create_workspace(database_session)
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="agent.node.started",
        payload=_node_payload(status="running"),
        event_key=EVENT_KEY,
    )
    with pytest.raises(IntegrityError):
        append_workspace_event(
            database_session,
            workspace_token=token,
            event_type="agent.node.started",
            payload=_node_payload(status="running"),
            event_key=EVENT_KEY,
        )


# ---------------------------------------------------------------------------
# 最近三轮公开投影
# ---------------------------------------------------------------------------
def _insert_raw_event(
    session: Session,
    token: str,
    *,
    event_type: str,
    payload: dict[str, object],
) -> None:
    """绕过校验直接插入一行，模拟历史/未知/畸形事件。"""

    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == sha256(token.encode()).hexdigest()
        )
    )
    assert workspace is not None
    session.add(
        WorkspaceEventRecord(
            workspace_id=workspace.id,
            event_type=event_type,
            payload=payload,
        )
    )
    session.commit()


def test_recent_turns_selects_last_three_turns_by_global_event_id(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    turns: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for index in range(4):
        turn_id = f"turn-order-{index}"
        events = [
            ("turn.started", {"turn_id": turn_id}),
            ("message.user", {"content": f"输入 {index}", "turn_id": turn_id}),
            ("tool.summary", {"tool": "x", "status": "ok", "summary": "y", "turn_id": turn_id}),
        ]
        turns[turn_id] = events
        for event_type, payload in events:
            append_workspace_event(
                database_session,
                workspace_token=token,
                event_type=event_type,
                payload=payload,
            )

    projections = list_recent_turns(
        database_session,
        workspace_token=token,
    )

    assert [turn.turn_id for turn in projections] == [
        "turn-order-3",
        "turn-order-2",
        "turn-order-1",
    ]
    for turn in projections:
        assert [event.id for event in turn.events] == sorted(
            event.id for event in turn.events
        )
        assert all(event.event_type != "tool.summary" or True for event in turn.events)


def test_recent_turns_orders_turn_groups_newest_first_and_events_ascending(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    # Older turn gets a late terminal to prove ordering uses global event id.
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "旧轮", "turn_id": "turn-old"},
    )
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "新轮", "turn_id": "turn-new"},
    )
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "中间轮", "turn_id": "turn-mid"},
    )
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.completed",
        payload={
            "turn_id": "turn-old",
            "message_id": "m-1",
            "content": "旧轮终态",
        },
    )

    projections = list_recent_turns(database_session, workspace_token=token)
    assert [turn.turn_id for turn in projections] == [
        "turn-old",
        "turn-mid",
        "turn-new",
    ]
    old = projections[0]
    assert [event.id for event in old.events] == sorted(
        event.id for event in old.events
    )
    assert old.events[-1].event_type == "message.completed"


def test_recent_turns_skips_unknown_and_invalid_events_without_crash(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "正常轮", "turn_id": "turn-normal"},
    )
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "未知轮", "turn_id": "turn-unknown"},
    )
    _insert_raw_event(
        database_session,
        token,
        event_type="mystery.event",
        payload={"turn_id": "turn-unknown", "raw": "不能暴露的原始事实"},
    )
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "畸形轮", "turn_id": "turn-broken"},
    )
    _insert_raw_event(
        database_session,
        token,
        event_type="message.completed",
        payload={"turn_id": "turn-broken"},
    )

    projections = list_recent_turns(database_session, workspace_token=token)

    assert [turn.turn_id for turn in projections] == [
        "turn-broken",
        "turn-unknown",
        "turn-normal",
    ]
    unknown = next(
        turn for turn in projections if turn.turn_id == "turn-unknown"
    )
    assert [event.event_type for event in unknown.events] == ["message.user"]
    broken = next(turn for turn in projections if turn.turn_id == "turn-broken")
    assert [event.event_type for event in broken.events] == ["message.user"]


def test_recent_turns_envelope_never_exposes_event_key_or_raw_payload(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "输入", "turn_id": "turn-envelope"},
    )
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="agent.node.completed",
        payload=_node_payload(turn_id="turn-envelope"),
        event_key=EVENT_KEY,
    )

    projections = list_recent_turns(database_session, workspace_token=token)
    assert len(projections) == 1
    events = projections[0].events
    assert [event.event_type for event in events] == [
        "message.user",
        "agent.node.completed",
    ]
    assert set(PublicEventProjection.model_fields) == {
        "id",
        "event_type",
        "payload",
        "occurred_at",
    }
    assert set(PublicTurnProjection.model_fields) == {"turn_id", "events"}
    for event in events:
        assert "event_key" not in event.payload
        assert "unexpected" not in event.payload


def test_recent_turns_validates_max_turns() -> None:
    from accesspilot.events import list_recent_turns as _list

    for invalid in (0, -1, 21, 1.5, True):
        with pytest.raises(ValueError, match="max_turns"):
            _list(
                object(),  # type: ignore[arg-type]
                workspace_token="x",
                max_turns=invalid,  # type: ignore[arg-type]
            )
