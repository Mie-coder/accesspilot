from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from accesspilot.db.models import WorkspaceRecord
from accesspilot.events import (
    ModelQuotaExceededError,
    UnsafeEventError,
    append_workspace_event,
    consume_model_call,
    format_sse_event,
    get_model_quota,
    list_workspace_events,
)


def create_workspace(
    session: Session,
    *,
    model_call_limit: int = 20,
) -> str:
    token = f"events-{uuid4()}"
    session.add(
        WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            model_call_limit=model_call_limit,
        )
    )
    session.commit()
    return token


def test_events_have_stable_ids_and_after_cursor_replays_only_missing(
    database_session: Session,
) -> None:
    token = create_workspace(database_session)
    first = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.user",
        payload={"content": "我要申请权限"},
    )
    second = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="tool.summary",
        payload={
            "tool": "validate_access_request",
            "status": "success",
            "summary": "申请字段与目录校验通过",
        },
    )
    third = append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="business.status",
        payload={"status": "awaiting_confirmation"},
    )

    replay = list_workspace_events(
        database_session,
        workspace_token=token,
        after_id=first.id,
    )

    assert first.id < second.id < third.id
    assert [event.id for event in replay] == [second.id, third.id]
    assert format_sse_event(second) == (
        f"id: {second.id}\n"
        "event: tool.summary\n"
        'data: {"tool":"validate_access_request","status":"success",'
        '"summary":"申请字段与目录校验通过"}\n\n'
    )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("model.reasoning", {"content": "隐藏推理"}),
        ("message.assistant", {"content": "安全回复", "chain_of_thought": "秘密"}),
        ("tool.summary", {"tool": "x", "status": "ok", "summary": "x", "api_key": "x"}),
    ],
)
def test_unknown_or_sensitive_event_payload_is_rejected(
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

    assert list_workspace_events(
        database_session,
        workspace_token=token,
    ) == []


def test_workspace_cannot_replay_another_workspace_events(
    database_session: Session,
) -> None:
    first_token = create_workspace(database_session)
    second_token = create_workspace(database_session)
    append_workspace_event(
        database_session,
        workspace_token=first_token,
        event_type="message.assistant",
        payload={"content": "只属于第一个 Workspace"},
    )

    assert list_workspace_events(
        database_session,
        workspace_token=second_token,
    ) == []


def test_quota_exhaustion_rejects_new_calls_but_history_stays_readable(
    database_session: Session,
) -> None:
    token = create_workspace(database_session, model_call_limit=2)
    append_workspace_event(
        database_session,
        workspace_token=token,
        event_type="message.assistant",
        payload={"content": "已有历史"},
    )

    first = consume_model_call(database_session, workspace_token=token)
    second = consume_model_call(database_session, workspace_token=token)
    with pytest.raises(ModelQuotaExceededError):
        consume_model_call(database_session, workspace_token=token)

    quota = get_model_quota(database_session, workspace_token=token)
    history = list_workspace_events(database_session, workspace_token=token)
    assert first.used == 1
    assert second.used == 2
    assert quota.used == 2
    assert quota.limit == 2
    assert quota.remaining == 0
    assert [event.payload for event in history] == [{"content": "已有历史"}]
