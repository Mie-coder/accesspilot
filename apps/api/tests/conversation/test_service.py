from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.state import ConversationPhase
from accesspilot.agent.structured_reply import MalformedStructuredOutputError
from accesspilot.conversation import handle_chat_message
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import ParsedReply
from accesspilot.events import (
    ModelQuotaExceededError,
    list_workspace_events,
)
from accesspilot.workspaces import WorkspaceService


class StaticStructuredReplyModel:
    def __init__(self, reply: ParsedReply) -> None:
        self.reply = reply
        self.calls: list[str] = []

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        self.calls.append(user_reply)
        return self.reply


class RetryingStructuredReplyModel:
    def __init__(self) -> None:
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        self.calls += 1
        if self.calls == 1:
            raise MalformedStructuredOutputError("第一次格式错误")
        return ParsedReply(employee_id="EMP-001")


def create_workspace(
    database_session_factory: sessionmaker[Session],
    *,
    model_call_limit: int = 20,
) -> tuple[str, WorkspaceService]:
    token = f"conversation-{uuid4()}"
    with database_session_factory() as session:
        seed_catalog(session)
        session.add(
            WorkspaceRecord(
                token_hash=sha256(token.encode()).hexdigest(),
                model_call_limit=model_call_limit,
            )
        )
        session.commit()
    service = WorkspaceService(SqlAlchemyWorkspaceStore(database_session_factory))
    return token, service


def test_chat_turn_merges_strict_reply_and_emits_only_safe_events(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_service = create_workspace(database_session_factory)
    model = StaticStructuredReplyModel(
        ParsedReply(
            employee_id="EMP-001",
            entitlement_id="insighthub.customer_export",
            duration_days=14,
            justification="核验虚构项目运营数据",
            confirmed=False,
        )
    )

    turn = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="我是 EMP-001，申请客户导出 14 天，用于核验数据",
        model=model,
    )

    assert turn.phase is ConversationPhase.AWAITING_CONFIRMATION
    assert turn.draft.employee_id == "EMP-001"
    assert turn.draft.entitlement_id == "insighthub.customer_export"
    assert turn.draft.confirmed is False
    assert turn.quota.used == 1
    with database_session_factory() as session:
        events = list_workspace_events(session, workspace_token=token)
    assert [event.event_type for event in events] == [
        "message.user",
        "draft.updated",
        "tool.summary",
        "business.status",
        "message.assistant",
    ]
    assert all("chain_of_thought" not in event.payload for event in events)


def test_exhausted_quota_rejects_model_and_keeps_history_readable(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_service = create_workspace(
        database_session_factory,
        model_call_limit=1,
    )
    model = StaticStructuredReplyModel(ParsedReply(employee_id="EMP-001"))
    handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="我是 EMP-001",
        model=model,
    )

    with pytest.raises(ModelQuotaExceededError):
        handle_chat_message(
            database_session_factory,
            workspace_service=workspace_service,
            workspace_token=token,
            content="再调用一次模型",
            model=model,
        )

    with database_session_factory() as session:
        history = list_workspace_events(session, workspace_token=token)
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == sha256(token.encode()).hexdigest()
            )
        )
    assert model.calls == ["我是 EMP-001"]
    assert history
    assert all(event.payload.get("content") != "再调用一次模型" for event in history)
    assert workspace is not None
    assert workspace.model_calls_used == 1


def test_correction_retry_consumes_a_second_model_call(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_service = create_workspace(
        database_session_factory,
        model_call_limit=2,
    )
    model = RetryingStructuredReplyModel()

    turn = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="我是 EMP-001",
        model=model,
    )

    assert model.calls == 2
    assert turn.quota.used == 2
    assert turn.quota.remaining == 0


def test_editing_confirmed_business_field_requires_new_confirmation(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_service = create_workspace(database_session_factory)
    model = StaticStructuredReplyModel(
        ParsedReply(
            employee_id="EMP-001",
            entitlement_id="insighthub.customer_export",
            duration_days=14,
            justification="核验虚构项目运营数据",
            confirmed=True,
        )
    )
    first = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="完整填写并确认提交",
        model=model,
    )
    assert first.draft.confirmed is True
    model.reply = ParsedReply(duration_days=7)

    edited = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="改成 7 天",
        model=model,
    )

    assert edited.draft.duration_days == 7
    assert edited.draft.confirmed is False
    assert edited.business_status == "awaiting_confirmation"
