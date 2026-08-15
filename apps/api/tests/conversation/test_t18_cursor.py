"""T18 红测：纯数字续答必须依赖服务端 ConversationCursor。"""

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

import accesspilot.conversation as conversation_module
from accesspilot.agent.routing import route_message
from accesspilot.conversation import (
    ConversationTurn,
    normalized_outcome,
)
from accesspilot.conversation import (
    apply_cursor_transition as _apply_cursor_transition,
)
from accesspilot.conversation import (
    handle_chat_message as _handle_chat_message,
)
from accesspilot.conversation import (
    prepare_chat_message as _prepare_chat_message,
)
from accesspilot.db.models import WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.domain.models import ConversationCursor, ParsedReply, RequestDraft
from accesspilot.events import ModelQuota
from accesspilot.streaming import encode_sse_frame
from accesspilot.tools.catalog import ToolResult
from accesspilot.workspaces import (
    DraftRevisionConflictError,
    InMemoryWorkspaceStore,
    WorkspaceService,
)

AUTH_SESSION_ID = "t18-test-auth-session"


def handle_chat_message(*args: Any, **kwargs: Any) -> ConversationTurn:
    """Use one explicit synthetic AuthSession for direct T18 service tests."""

    kwargs.setdefault("auth_session_id", AUTH_SESSION_ID)
    return _handle_chat_message(*args, **kwargs)


def prepare_chat_message(*args: Any, **kwargs: Any) -> ConversationTurn:
    kwargs.setdefault("auth_session_id", AUTH_SESSION_ID)
    return _prepare_chat_message(*args, **kwargs)


def apply_cursor_transition(*args: Any, **kwargs: Any) -> None:
    kwargs.setdefault("auth_session_id", AUTH_SESSION_ID)
    _apply_cursor_transition(*args, **kwargs)


class StaticReplyModel:
    def __init__(self, reply: ParsedReply) -> None:
        self.reply = reply
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        return self.reply


class RacingReplyModel:
    """模型回调期间并发写入新事实，再返回一份过期旧回复。"""

    def __init__(self, service: WorkspaceService, token: str) -> None:
        self.service = service
        self.token = token
        self.calls = 0

    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        del user_reply, correction
        self.calls += 1
        concurrent = self.service.get(
            self.token,
            auth_session_id=AUTH_SESSION_ID,
        )
        self.service.save_draft_cas(
            self.token,
            expected_revision=concurrent.draft_revision,
            draft=RequestDraft(
                employee_id="EMP-001",
                entitlement_id="insighthub.customer_export",
                justification="并发写入的新业务理由",
            ),
            auth_session_id=AUTH_SESSION_ID,
        )
        return ParsedReply(duration_days=14)


class ExplodingReplyModel:
    def parse_reply(
        self,
        user_reply: str,
        correction: str | None = None,
    ) -> ParsedReply:
        raise AssertionError(f"numeric follow-up must not call model: {user_reply}")


class FakeSession:
    def __init__(self, *, max_duration_days: int | None = None) -> None:
        self.max_duration_days = max_duration_days

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def get(self, model: object, key: object) -> object | None:
        del model, key
        if self.max_duration_days is None:
            return SimpleNamespace(max_duration_days=None)
        return SimpleNamespace(max_duration_days=self.max_duration_days)


class FakeSessionFactory:
    def __init__(self, *, max_duration_days: int | None = None) -> None:
        self.max_duration_days = max_duration_days

    def __call__(self) -> FakeSession:
        return FakeSession(max_duration_days=self.max_duration_days)


def isolated_numeric_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_duration_days: int | None = 180,
) -> tuple[str, WorkspaceService, FakeSessionFactory]:
    """用内存 Workspace 验证数字路径，不依赖外部数据库。"""

    session_factory = FakeSessionFactory(max_duration_days=max_duration_days)
    monkeypatch.setattr(
        conversation_module,
        "get_model_quota",
        lambda session, *, workspace_token: ModelQuota(
            used=0, limit=20, remaining=20, retry_consumed=0
        ),
    )
    monkeypatch.setattr(conversation_module, "_append_event", lambda *args, **kwargs: None)
    service = WorkspaceService(InMemoryWorkspaceStore())
    workspace = service.create()
    service.save_draft(
        workspace.token,
        RequestDraft(
            employee_id="EMP-001",
            entitlement_id="insighthub.customer_export",
            confirmed=False,
        ),
    )
    service.activate_cursor(
        workspace.token,
        expected_revision=1,
        expected_field="duration_days",
        last_question_kind="duration_days",
        auth_session_id=AUTH_SESSION_ID,
    )
    return workspace.token, service, session_factory


def create_workspace(
    database_session_factory: sessionmaker[Session],
) -> tuple[str, WorkspaceService]:
    token = f"t18-cursor-{uuid4()}"
    with database_session_factory() as session:
        seed_catalog(session)
        session.add(
            WorkspaceRecord(token_hash=sha256(token.encode()).hexdigest())
        )
        session.commit()
    return token, WorkspaceService(SqlAlchemyWorkspaceStore(database_session_factory))


def test_bare_numeric_input_is_not_routed_to_help() -> None:
    """红测：v1.1 当前把没有上下文的纯数字错误归为 help。"""

    assert route_message("111").intent == "unknown"


def test_numeric_follow_up_uses_duration_cursor_instead_of_help(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_service = create_workspace(database_session_factory)
    model = StaticReplyModel(
        ParsedReply(entitlement_id="insighthub.dashboard_view")
    )

    first = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="申请 insighthub.dashboard_view",
        model=model,
    )
    assert first.business_status == "collecting"
    assert first.missing_fields == ["duration_days", "justification"]

    follow_up = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="111",
        model=model,
    )

    assert follow_up.intent == "request_access"
    assert follow_up.business_status == "collecting"
    assert follow_up.draft.duration_days == 111
    assert model.calls == 1


@pytest.mark.parametrize(
    ("content", "expected_justification", "expected_status", "expected_cursor"),
    [
        (
            "演示测试流程申请",
            "演示测试流程申请",
            "awaiting_confirmation",
            "confirmation",
        ),
        ("111", None, "collecting", "justification"),
        ("帮助", None, "answered", None),
        ("今天天气怎么样", None, "answered", None),
        ("测试环境怎么使用？", None, "answered", None),
        ("测试环境如何使用", None, "answered", None),
        ("测试环境是什么", None, "answered", None),
        ("测试哪些流程", None, "answered", None),
        ("测试环境为什么失败", None, "answered", None),
        ("测试环境为何失败", None, "answered", None),
        ("测试环境能否使用", None, "answered", None),
        ("测试环境是否可用", None, "answered", None),
        ("测试环境可用吗", None, "answered", None),
        ("测试环境如何使用呢", None, "answered", None),
        ("用于什么业务？", None, "answered", None),
        (
            "用于测试如何申请权限的演示",
            "用于测试如何申请权限的演示",
            "awaiting_confirmation",
            "confirmation",
        ),
        ("employee_id=EMP-003", None, "answered", "justification"),
    ],
)
def test_justification_cursor_accepts_only_safe_plain_language_without_model(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_justification: str | None,
    expected_status: str,
    expected_cursor: str | None,
) -> None:
    """理由 Cursor 仅消费切题、安全的自然语言，不能依赖“用于/为了”。"""

    token, workspace_service, session_factory = isolated_numeric_context(monkeypatch)
    monkeypatch.setattr(
        conversation_module,
        "validate_access_request",
        lambda session, draft: ToolResult(status="success"),
    )
    workspace_service.save_draft(
        token,
        RequestDraft(
            employee_id="EMP-001",
            entitlement_id="insighthub.customer_export",
            duration_days=14,
            confirmed=False,
        ),
        auth_session_id=AUTH_SESSION_ID,
    )
    workspace_service.activate_cursor(
        token,
        expected_revision=2,
        expected_field="justification",
        last_question_kind="justification",
        auth_session_id=AUTH_SESSION_ID,
    )

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=workspace_service,
        workspace_token=token,
        content=content,
        model=ExplodingReplyModel(),
    )

    assert turn.draft.justification == expected_justification
    assert turn.business_status == expected_status
    cursor = workspace_service.get(
        token,
        auth_session_id=AUTH_SESSION_ID,
    ).active_cursor()
    assert (cursor.expected_field if cursor is not None else None) == expected_cursor
    if expected_justification is not None:
        assert turn.intent == "request_access"
        assert turn.missing_fields == []


def test_numeric_message_without_active_cursor_needs_clarification_without_model_call(
    database_session_factory: sessionmaker[Session],
) -> None:
    token, workspace_service = create_workspace(database_session_factory)
    model = StaticReplyModel(ParsedReply())

    turn = handle_chat_message(
        database_session_factory,
        workspace_service=workspace_service,
        workspace_token=token,
        content="111",
        model=model,
    )

    assert turn.intent == "unknown"
    assert turn.business_status == "needs_clarification"
    assert model.calls == 0
    assert workspace_service.get(token).draft is None


def test_duration_cursor_accepts_111_up_to_catalog_limit_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
    )

    assert turn.intent == "request_access"
    assert turn.business_status == "collecting"
    assert turn.draft.duration_days == 111
    assert turn.draft_revision == 2
    cursor = service.get(token).active_cursor()
    assert cursor is not None
    assert cursor.expected_field == "justification"


def test_repeating_111_cannot_consume_the_same_duration_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)
    first = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
    )
    assert first.draft.duration_days == 111
    revision_after_first = service.get(token).draft_revision

    second = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
    )

    assert second.intent == "request_access"
    assert second.business_status == "collecting"
    assert second.draft.duration_days == 111
    assert service.get(token).draft_revision == revision_after_first


def test_stream_prepare_does_not_activate_next_cursor_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)

    turn = prepare_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
        turn_id="turn-t18",
    )

    assert turn.draft.duration_days == 111
    assert service.get(token).active_cursor() is None
    apply_cursor_transition(
        service,
        workspace_token=token,
        turn=turn,
    )
    cursor = service.get(token).active_cursor()
    assert cursor is not None
    assert cursor.expected_field == "justification"


def test_duration_cursor_rejects_catalog_over_limit_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(
        monkeypatch,
        max_duration_days=30,
    )
    before = service.get(token)
    before_cursor = before.active_cursor()
    assert before_cursor is not None

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
    )

    after = service.get(token)
    assert turn.business_status == "collecting"
    assert turn.error_code == "DURATION_EXCEEDS_MAXIMUM"
    assert after.draft == before.draft
    assert after.draft_revision == before.draft_revision == 1
    assert after.active_cursor() == before_cursor


@pytest.mark.parametrize(
    ("expected_field", "expected_status", "expected_code"),
    [
        ("entitlement_id", "collecting", "ENTITLEMENT_REQUIRED"),
        ("justification", "collecting", "JUSTIFICATION_REQUIRED"),
        ("confirmation", "awaiting_confirmation", "CONFIRMATION_REQUIRED"),
    ],
)
def test_numeric_follow_up_respects_non_duration_cursor(
    monkeypatch: pytest.MonkeyPatch,
    expected_field: str,
    expected_status: str,
    expected_code: str,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)
    service.activate_cursor(
        token,
        expected_revision=1,
        expected_field=expected_field,
        last_question_kind=expected_field,
        auth_session_id=AUTH_SESSION_ID,
    )

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
    )

    assert turn.intent == "request_access"
    assert turn.business_status == expected_status
    assert turn.error_code == expected_code
    assert turn.draft_revision == 1
    assert service.get(token).draft is not None
    assert service.get(token).draft.duration_days is None
    assert service.get(token).active_cursor() is not None
    assert service.get(token).active_cursor().expected_field == expected_field  # type: ignore[union-attr]


@pytest.mark.parametrize("invalid", ["0", "-1", "1.5", "12345"])
def test_invalid_duration_keeps_cursor_and_revision(
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)
    before = service.get(token)

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content=invalid,
        model=ExplodingReplyModel(),
    )

    after = service.get(token)
    assert turn.business_status == "collecting"
    assert turn.error_code == "INVALID_DURATION_DAYS"
    assert after.draft_revision == before.draft_revision == 1
    assert after.draft == before.draft
    assert after.active_cursor() == before.active_cursor()


def test_help_has_priority_and_clears_active_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="帮助",
        model=ExplodingReplyModel(),
    )

    assert turn.intent == "help"
    assert turn.business_status == "answered"
    assert service.get(token).active_cursor() is None


def test_json_non_request_tool_failure_still_clears_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)

    def fail_tool(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("read-only source unavailable")

    monkeypatch.setattr(conversation_module, "execute_read_only_tool", fail_tool)
    with pytest.raises(RuntimeError, match="read-only source unavailable"):
        handle_chat_message(
            session_factory,  # type: ignore[arg-type]
            workspace_service=service,
            workspace_token=token,
            content="政策有哪些",
            model=ExplodingReplyModel(),
        )

    assert service.get(token).active_cursor() is None


@pytest.mark.parametrize(
    ("content", "expected_intent"),
    [
        ("政策有哪些", "policy_question"),
        ("我能申请什么", "discover_eligible_access"),
        ("我现在有什么权限", "list_active_access"),
        ("我的申请状态", "request_status"),
    ],
)
def test_explicit_non_request_question_clears_cursor_before_numeric_follow_up(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_intent: str,
) -> None:
    """切出申请收集后，旧期限 Cursor 不得污染下一轮纯数字输入。"""

    token, service, session_factory = isolated_numeric_context(monkeypatch)
    monkeypatch.setattr(
        conversation_module,
        "execute_read_only_tool",
        lambda *args, **kwargs: None,
    )
    before = service.get(token)
    assert before.active_cursor() is not None

    question_turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content=content,
        model=ExplodingReplyModel(),
    )

    assert question_turn.intent == expected_intent
    assert question_turn.business_status == "answered"
    after_question = service.get(token)
    assert after_question.active_cursor() is None
    assert after_question.draft == before.draft
    assert after_question.draft_revision == before.draft_revision

    follow_up = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="111",
        model=ExplodingReplyModel(),
    )

    assert follow_up.intent == "unknown"
    assert follow_up.business_status == "needs_clarification"
    after_numeric = service.get(token)
    assert after_numeric.draft == before.draft
    assert after_numeric.draft_revision == before.draft_revision


def test_stale_draft_revision_is_rejected_by_cas() -> None:
    service = WorkspaceService(InMemoryWorkspaceStore())
    workspace = service.create()
    service.save_draft(workspace.token, RequestDraft(employee_id="EMP-001"))

    with pytest.raises(DraftRevisionConflictError):
        service.save_draft_cas(
            workspace.token,
            expected_revision=0,
            draft=RequestDraft(employee_id="EMP-001", duration_days=7),
            auth_session_id=AUTH_SESSION_ID,
        )


def test_model_reply_cannot_overwrite_a_concurrent_newer_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)
    monkeypatch.setattr(
        conversation_module,
        "consume_model_call",
        lambda session, *, workspace_token, is_retry=False: ModelQuota(
            used=1,
            limit=20,
            remaining=19,
            retry_consumed=1 if is_retry else 0,
        ),
    )
    model = RacingReplyModel(service, token)

    turn = handle_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="申请期限",
        model=model,
    )

    assert model.calls == 1
    assert turn.business_status == "recoverable_error"
    assert turn.phase.value == "recoverable_error"
    assert turn.error_code == "DRAFT_REVISION_CONFLICT"
    assert turn.draft_revision == 2
    saved = service.get(token)
    assert saved.draft_revision == 2
    assert saved.draft is not None
    assert saved.draft.justification == "并发写入的新业务理由"
    assert saved.draft.duration_days is None
    assert saved.active_cursor() is None


def test_model_reply_conflict_is_recoverable_during_stream_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, service, session_factory = isolated_numeric_context(monkeypatch)
    monkeypatch.setattr(
        conversation_module,
        "consume_model_call",
        lambda session, *, workspace_token, is_retry=False: ModelQuota(
            used=1,
            limit=20,
            remaining=19,
            retry_consumed=1 if is_retry else 0,
        ),
    )

    turn = prepare_chat_message(
        session_factory,  # type: ignore[arg-type]
        workspace_service=service,
        workspace_token=token,
        content="申请期限",
        model=RacingReplyModel(service, token),
        turn_id="turn-t18-conflict",
    )

    assert turn.business_status == "recoverable_error"
    assert turn.phase.value == "recoverable_error"
    assert turn.error_code == "DRAFT_REVISION_CONFLICT"
    assert service.get(token).draft_revision == 2


def test_cursor_activation_cannot_overwrite_a_concurrent_draft_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryWorkspaceStore()
    service = WorkspaceService(store)
    workspace = service.create()
    original_get = service.get
    raced = False

    def get_with_concurrent_draft(
        token: str,
        *,
        auth_session_id: str | None = None,
    ):
        nonlocal raced
        snapshot = deepcopy(original_get(token, auth_session_id=auth_session_id))
        if not raced:
            raced = True
            current = store.get(token)
            assert current is not None
            current.draft = RequestDraft(
                employee_id="EMP-001",
                justification="并发更新后的新理由",
            )
            current.draft_revision += 1
            current.clear_cursor()
            store.save(current)
        return snapshot

    monkeypatch.setattr(service, "get", get_with_concurrent_draft)
    with pytest.raises(DraftRevisionConflictError):
        service.activate_cursor(
            workspace.token,
            expected_revision=0,
            expected_field="duration_days",
            last_question_kind="duration_days",
            auth_session_id=AUTH_SESSION_ID,
        )

    saved = WorkspaceService(store).get(workspace.token)
    assert saved.draft_revision == 1
    assert saved.draft is not None
    assert saved.draft.justification == "并发更新后的新理由"
    assert saved.active_cursor() is None


def test_cursor_clear_cannot_overwrite_a_concurrent_draft_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryWorkspaceStore()
    service = WorkspaceService(store)
    workspace = service.create()
    service.activate_cursor(
        workspace.token,
        expected_revision=0,
        expected_field="duration_days",
        last_question_kind="duration_days",
        auth_session_id=AUTH_SESSION_ID,
    )
    original_get = service.get
    raced = False

    def get_with_concurrent_draft(
        token: str,
        *,
        auth_session_id: str | None = None,
    ):
        nonlocal raced
        snapshot = deepcopy(original_get(token, auth_session_id=auth_session_id))
        if not raced:
            raced = True
            current = store.get(token)
            assert current is not None
            current.draft = RequestDraft(
                employee_id="EMP-001",
                justification="并发更新后的新理由",
            )
            current.draft_revision += 1
            current.clear_cursor()
            store.save(current)
        return snapshot

    monkeypatch.setattr(service, "get", get_with_concurrent_draft)
    with pytest.raises(DraftRevisionConflictError):
        service.clear_cursor(
            workspace.token,
            auth_session_id=AUTH_SESSION_ID,
        )

    saved = WorkspaceService(store).get(workspace.token)
    assert saved.draft_revision == 1
    assert saved.draft is not None
    assert saved.draft.justification == "并发更新后的新理由"
    assert saved.active_cursor() is None


def test_cursor_activation_rejects_a_cursor_bound_to_another_actor() -> None:
    store = InMemoryWorkspaceStore()
    service = WorkspaceService(store)
    workspace = service.create()
    stale_cursor = ConversationCursor(
        workspace_id=workspace.workspace_id or workspace.token,
        actor_id="EMP-002",
        auth_session_id=AUTH_SESSION_ID,
        draft_revision=0,
        expected_field="duration_days",
        last_question_kind="duration_days",
        issued_at=datetime.now(UTC),
    )

    assert not store.activate_cursor_cas(
        workspace.token,
        expected_revision=0,
        cursor=stale_cursor,
        auth_session_id=AUTH_SESSION_ID,
    )
    assert service.get(workspace.token).active_cursor() is None


def test_normalized_json_outcome_can_be_rebuilt_in_sse_terminal_payload() -> None:
    import json

    from accesspilot.events import MessageCompletedPayload

    turn = ConversationTurn(
        assistant_message="请提供申请理由。",
        draft=RequestDraft(employee_id="EMP-001"),
        missing_fields=["entitlement_id", "duration_days", "justification"],
        phase="collecting",
        business_status="collecting",
        quota=ModelQuota(used=0, limit=20, remaining=20, retry_consumed=0),
        intent="request_access",
        draft_revision=1,
    )
    outcome = normalized_outcome(turn)
    payload = {
        "turn_id": "turn-1",
        "message_id": "message-1",
        "content": turn.assistant_message,
        **outcome,
    }
    validated = MessageCompletedPayload.model_validate(payload)
    frame = encode_sse_frame(
        event_type="message.completed",
        turn_id="turn-1",
        seq=1,
        payload=validated.model_dump(mode="json"),
    )
    data = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data:")))
    assert data["payload"]["intent"] == outcome["intent"]
    assert data["payload"]["business_status"] == outcome["business_status"]
    assert data["payload"]["draft_revision"] == outcome["draft_revision"]
    assert data["payload"]["draft"] == outcome["draft"]
    assert data["payload"]["assistant_message"] == outcome["assistant_message"]
