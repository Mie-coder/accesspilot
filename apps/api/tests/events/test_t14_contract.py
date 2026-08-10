"""T14 Workspace 事件合同红测。"""

import pytest
from sqlalchemy.orm import Session

from accesspilot.events import (
    TurnInProgressError,
    UnsafeEventError,
    append_turn_started,
    append_turn_terminal,
    append_workspace_event,
    list_turn_events,
)

from .test_store import create_workspace


def test_terminal_events_are_safe_and_delta_is_not_persistable(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    completed = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.completed",
        payload={
            "turn_id": "turn-complete",
            "message_id": "message-1",
            "content": "完整回答",
        },
    )
    interrupted = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="turn.interrupted",
        payload={
            "turn_id": "turn-interrupted",
            "reason": "client_cancelled",
            "retryable": True,
        },
    )

    assert completed.payload["turn_id"] == "turn-complete"
    assert interrupted.payload["turn_id"] == "turn-interrupted"
    with pytest.raises(UnsafeEventError):
        append_workspace_event(
            database_session,
            workspace_token=token,
            event_type="message.delta",
            payload={
                "turn_id": "turn-complete",
                "message_id": "message-1",
                "index": 0,
                "text": "不可回放的增量",
            },
        )

def test_workspace_rejects_a_second_active_turn(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)

    append_turn_started(
        database_session,
        workspace_token=token,
        turn_id="turn-active",
    )

    with pytest.raises(TurnInProgressError):
        append_turn_started(
            database_session,
            workspace_token=token,
            turn_id="turn-second",
        )


def test_terminal_event_is_compare_and_set_and_second_terminal_is_a_noop(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)

    first = append_turn_terminal(
        database_session,
        workspace_token=token,
        turn_id="turn-terminal",
        event_type="message.completed",
        payload={
            "turn_id": "turn-terminal",
            "message_id": "message-1",
            "content": "完整回答",
        },
    )
    second = append_turn_terminal(
        database_session,
        workspace_token=token,
        turn_id="turn-terminal",
        event_type="turn.interrupted",
        payload={
            "turn_id": "turn-terminal",
            "reason": "client_cancelled",
            "retryable": True,
        },
    )

    assert first is not None
    assert second is None
    events = list_turn_events(
        database_session,
        workspace_token=token,
        turn_id="turn-terminal",
    )
    assert [event.event_type for event in events] == ["message.completed"]
