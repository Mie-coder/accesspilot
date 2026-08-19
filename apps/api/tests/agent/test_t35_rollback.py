"""T35 publish preflight gate and Legacy rollback bridge tests.

The fixtures produce real confirmation interrupt checkpoints (production graph
+ InMemorySaver) or crafted exact-head tuples, then build the application
projection rows exactly as ``finalize_interrupt`` would.  The tests cover the
two-way pending/checkpoint reconciliation, the three executable rollback
mappings (clean confirmation, principal/revision conflict, no pending), every
blocking path (unreadable, unknown kind, mismatched projection, orphan
accepted head, unfinished execution) and dry-run purity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.production_graph import (
    ConfirmationInterruptRaised,
    GraphInput,
    GraphRuntimeContext,
    build_production_graph,
)
from accesspilot.agent.rollback import (
    CheckpointTaskReader,
    LegacyRollbackBridge,
    PublishPreflightGate,
    RollbackBlockedError,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.turn_execution import TurnExecutionService
from accesspilot.auth import Principal
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import (
    SqlAlchemyWorkspaceStore,
    hash_workspace_token,
)
from accesspilot.domain.models import ParsedReply, RequestDraft
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import WorkspaceService


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


@dataclass(frozen=True)
class T35RollbackFixture:
    token: str
    workspace_id: UUID
    agent_thread_id: UUID
    auth_session_id: UUID
    graph_run_id: UUID
    thread_id: str
    head_id: str
    pending_input_id: UUID
    saver: InMemorySaver
    input_turn_id: str


def _complete_draft() -> RequestDraft:
    return RequestDraft(
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="业务需要",
        confirmed=False,
    )


def _request_text() -> str:
    return "申请客户数据导出 30 天，用于业务需要"


def _config_for(thread_id: str) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }


def _rollback_fixture(
    factory: sessionmaker[Session],
    *,
    revoke_auth: bool = False,
    draft_revision_override: int | None = None,
) -> T35RollbackFixture:
    """Run the production graph to a real confirmation interrupt and persist
    the application projection exactly as ``finalize_interrupt`` does."""
    token = f"t35-rollback-{uuid4()}"
    auth_session_id = uuid4()
    graph_run_id = uuid4()
    input_turn_id = f"turn-{uuid4()}"
    pending_input_id = uuid4()
    draft = _complete_draft()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=2,
            lease_fence=1,
            draft=draft.model_dump(mode="json"),
            draft_revision=1,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        session.add(
            AuthSessionRecord(
                id=auth_session_id,
                token_hash=sha256(f"auth-{auth_session_id}".encode()).hexdigest(),
                csrf_hash=sha256(f"csrf-{auth_session_id}".encode()).hexdigest(),
                employee_id="EMP-001",
                workspace_id=workspace_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        input_event = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.user",
            payload={"content": _request_text(), "turn_id": input_turn_id},
        )
        session.add(input_event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=workspace_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                input_seq=0,
                input_turn_id=input_turn_id,
                input_event_id=input_event.id,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                status="running",
                checkpoint_ns="",
            )
        )
        session.commit()
        workspace_id = workspace.id
    workspace_service = WorkspaceService(SqlAlchemyWorkspaceStore(factory))
    context: GraphRuntimeContext = {
        "session_factory": factory,
        "workspace_service": workspace_service,
        "policy_service": PolicyService(embedding_model=DeterministicEmbeddingModel()),
        "structured_reply_model": StaticReplyModel(
            ParsedReply(
                entitlement_id="insighthub.customer_export",
                duration_days=30,
                justification="业务需要",
            )
        ),
        "intent_router": DeterministicIntentRouter(),
        "principal": Principal(
            employee_id="EMP-001",
            name="林晓",
            department="数据平台部",
            roles=("analyst",),
        ),
        "current_turn_id": input_turn_id,
        "current_fence": 1,
        "workspace_token": token,
        "auth_session_id": str(auth_session_id),
        "cookie": "runtime-cookie",
        "csrf_token": "runtime-csrf",
        "api_key": "runtime-key",
        "pending_input_id": str(pending_input_id),
    }
    graph_input = GraphInput(
        schema_version=1,
        flow_version=2,
        workspace_ref=workspace_id,
        graph_run_id=graph_run_id,
        input_seq=0,
        input_turn_id=input_turn_id,
        safe_user_text=_request_text(),
        input_kind="new_input",
    )
    saver = InMemorySaver()
    graph = build_production_graph(checkpointer=saver)
    with pytest.raises(ConfirmationInterruptRaised):
        graph.invoke(
            graph_input,
            _config_for(f"accesspilot:v1.3:{graph_run_id}"),
            context=context,
        )
    head_tuple = saver.get_tuple(_config_for(f"accesspilot:v1.3:{graph_run_id}"))
    assert head_tuple is not None
    head_id = cast(
        str, head_tuple.config["configurable"]["checkpoint_id"]
    )
    # Persist the accepted projection rows exactly like finalize_interrupt.
    with factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(token)
            )
        )
        assert workspace is not None
        if revoke_auth:
            auth = session.get(AuthSessionRecord, auth_session_id)
            assert auth is not None
            auth.revoked_at = datetime.now(UTC)
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == workspace_id,
                AgentTurnExecutionRecord.graph_run_id == graph_run_id,
            )
        )
        assert execution is not None
        terminal = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.completed",
            payload={
                "turn_id": input_turn_id,
                "message_id": f"msg-{uuid4()}",
                "content": "申请信息已完整。请明确回复“确认提交”后再创建正式申请。",
                "intent": "request_access",
                "business_status": "awaiting_confirmation",
                "draft_revision": 1,
            },
        )
        session.add(terminal)
        session.flush()
        execution.status = "waiting_input"
        execution.lease_expires_at = None
        execution.accepted_checkpoint_id = head_id
        execution.terminal_event_id = terminal.id
        execution.updated_at = datetime.now(UTC)
        workspace.cursor_actor_id = "EMP-001"
        workspace.cursor_auth_session_id = str(auth_session_id)
        workspace.cursor_expected_field = "confirmation"
        workspace.cursor_last_question_kind = "confirmation"
        workspace.cursor_issued_at = datetime.now(UTC)
        workspace.cursor_consumed_at = None
        workspace.draft_revision = (
            draft_revision_override
            if draft_revision_override is not None
            else workspace.draft_revision
        )
        session.add(
            AgentPendingInputRecord(
                workspace_id=workspace_id,
                agent_thread_id=workspace.agent_thread_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                pending_input_id=pending_input_id,
                kind="confirmation",
                draft_revision=1,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                checkpoint_ns="",
                accepted_checkpoint_id=head_id,
                status="active",
            )
        )
        session.commit()
    return T35RollbackFixture(
        token,
        workspace_id,
        workspace.agent_thread_id,
        auth_session_id,
        graph_run_id,
        f"accesspilot:v1.3:{graph_run_id}",
        head_id,
        pending_input_id,
        saver,
        input_turn_id,
    )


def _craft_head(
    saver: InMemorySaver,
    *,
    thread_id: str,
    head_id: str,
    kind: str,
    pending_input_id: UUID,
    draft_revision: int,
    graph_run_id: UUID,
    workspace_ref: UUID,
    schema_version: int = 1,
    flow_version: int = 2,
    extra_interrupt_payloads: Sequence[dict[str, object]] | None = None,
) -> None:
    """Write an exact checkpoint head carrying a crafted interrupt task.

    ``extra_interrupt_payloads`` adds further ``__interrupt__`` writes to the
    same head (used to prove that multiple retained tasks fail closed).
    """
    values: dict[str, Any] = {
        "schema_version": schema_version,
        "flow_version": flow_version,
        "workspace_ref": workspace_ref,
        "graph_run_id": graph_run_id,
        "input_seq": 0,
        "input_turn_id": f"turn-{uuid4()}",
        "base_draft_revision": draft_revision,
        "committed_draft_revision": draft_revision,
    }
    new_versions = {
        key: f"00000000000000000000000000000001.0.{index}"
        for index, key in enumerate(values)
    }
    config: dict[str, object] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": head_id,
        }
    }
    saver.put(
        config,
        {
            "v": 1,
            "id": head_id,
            "ts": datetime.now(UTC).isoformat(),
            "channel_values": values,
            "channel_versions": new_versions,
            "versions_seen": {},
            "updated_channels": None,
        },
        {"source": "loop", "step": 7},
        new_versions,
    )
    saver.put_writes(
        config,
        [
            (
                "__interrupt__",
                (
                    Interrupt(
                        value={
                            "kind": kind,
                            "pending_input_id": str(pending_input_id),
                            "draft_revision": draft_revision,
                            "summary": "crafted",
                            "allowed_decisions": ["confirm", "route_new_input"],
                        }
                    ),
                ),
            )
        ],
        task_id="task-interrupt",
        task_path="",
    )
    for index, payload in enumerate(extra_interrupt_payloads or ()):
        saver.put_writes(
            config,
            [("__interrupt__", (Interrupt(value=payload),))],
            task_id=f"task-extra-{index}",
            task_path="",
        )


def _craft_plain_head(
    saver: InMemorySaver,
    *,
    thread_id: str,
    head_id: str,
    graph_run_id: UUID,
    workspace_ref: UUID,
    input_seq: int = 1,
) -> None:
    """Write an exact checkpoint head without any interrupt task (a normal
    END/continuation head)."""
    values: dict[str, Any] = {
        "schema_version": 1,
        "flow_version": 2,
        "workspace_ref": workspace_ref,
        "graph_run_id": graph_run_id,
        "input_seq": input_seq,
        "input_turn_id": f"turn-{uuid4()}",
        "base_draft_revision": 1,
        "committed_draft_revision": 1,
    }
    new_versions = {
        key: f"00000000000000000000000000000001.0.{index}"
        for index, key in enumerate(values)
    }
    config: dict[str, object] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": head_id,
        }
    }
    saver.put(
        config,
        {
            "v": 1,
            "id": head_id,
            "ts": datetime.now(UTC).isoformat(),
            "channel_values": values,
            "channel_versions": new_versions,
            "versions_seen": {},
            "updated_channels": None,
        },
        {"source": "loop", "step": 8},
        new_versions,
    )


def _add_superseding_continuation(
    factory: sessionmaker[Session],
    fixture: T35RollbackFixture,
    saver: InMemorySaver,
) -> str:
    """Add a later terminal execution of the same run whose accepted END head
    retains no interrupt task: the proof that a resolved pending's interrupt
    was consumed by a successful resume."""
    end_head = "ad" * 16
    _craft_plain_head(
        saver,
        thread_id=fixture.thread_id,
        head_id=end_head,
        graph_run_id=fixture.graph_run_id,
        workspace_ref=fixture.workspace_id,
    )
    with factory() as session:
        input_event = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="message.user",
            payload={"content": "确认提交", "turn_id": f"turn-{uuid4()}"},
        )
        session.add(input_event)
        session.flush()
        terminal = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="message.completed",
            payload={
                "turn_id": f"turn-{uuid4()}",
                "message_id": f"msg-{uuid4()}",
                "content": "申请信息已明确确认，可以提交正式申请。",
                "intent": "request_access",
                "business_status": "ready_to_submit",
                "draft_revision": 1,
            },
        )
        session.add(terminal)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=fixture.workspace_id,
                graph_run_id=fixture.graph_run_id,
                checkpoint_thread_id=fixture.thread_id,
                input_seq=1,
                input_turn_id=f"turn-{uuid4()}",
                input_event_id=input_event.id,
                auth_session_ref=fixture.auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=None,
                status="completed",
                checkpoint_ns="",
                accepted_checkpoint_id=end_head,
                terminal_event_id=terminal.id,
            )
        )
        session.commit()
    return end_head


def _reader(saver: InMemorySaver) -> CheckpointTaskReader:
    return CheckpointTaskReader(saver)


def _bridge(
    factory: sessionmaker[Session],
    saver: InMemorySaver,
) -> LegacyRollbackBridge:
    return LegacyRollbackBridge(factory, _reader(saver))


def _pending_row(
    factory: sessionmaker[Session],
    pending_input_id: UUID,
) -> AgentPendingInputRecord | None:
    with factory() as session:
        return session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id == pending_input_id
            )
        )


def _workspace_row(
    factory: sessionmaker[Session],
    workspace_id: UUID,
) -> WorkspaceRecord | None:
    with factory() as session:
        return session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == workspace_id)
        )


# ---------------------------------------------------------------------------
# Executable rollback mappings
# ---------------------------------------------------------------------------
def test_rollback_reconciled_confirmation_abandons_to_legacy_atomically(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.conflict is False
    assert report.flow_version_before == 2
    assert report.flow_version_after == 1
    assert len(report.pending_outcomes) == 1
    outcome = report.pending_outcomes[0]
    assert outcome.mapping == "abandoned_to_legacy"
    assert outcome.retired_at is not None
    assert outcome.retirement_reason

    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.flow_version == 1
        # The confirmation Cursor is created/kept for the Legacy path.
        assert workspace.cursor_expected_field == "confirmation"
        assert workspace.cursor_last_question_kind == "confirmation"
        assert workspace.cursor_consumed_at is None
        assert workspace.cursor_auth_session_id == str(fixture.auth_session_id)
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        assert pending.status == "abandoned_to_legacy"
        assert pending.retired_at is not None
        assert pending.retirement_reason
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        assert execution.status == "waiting_input"
        assert execution.terminal_event_id is not None

    # The old accepted head is retained and still readable; the bridge never
    # deletes or downgrades checkpoint data.
    locator_config = {
        "configurable": {
            "thread_id": fixture.thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": fixture.head_id,
        }
    }
    assert fixture.saver.get_tuple(locator_config) is not None

    # The mapping is idempotent: a second execution changes nothing further.
    second = bridge.execute(workspace_token=fixture.token)
    assert not second.blocked
    assert second.flow_version_after == 1
    assert second.pending_outcomes == ()


def test_rollback_revision_mismatch_marks_abandoned_conflict_and_clears_cursor(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(
        database_session_factory, draft_revision_override=2
    )
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.conflict is True
    assert report.flow_version_after == 1
    assert report.pending_outcomes[0].mapping == "abandoned_conflict"
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.flow_version == 1
        assert workspace.cursor_expected_field is None
        assert workspace.cursor_auth_session_id is None
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        assert pending.status == "abandoned_conflict"
        assert pending.retired_at is not None


def test_rollback_principal_mismatch_marks_abandoned_conflict_and_clears_cursor(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(
        database_session_factory, revoke_auth=True
    )
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.conflict is True
    assert report.pending_outcomes[0].mapping == "abandoned_conflict"
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.flow_version == 1
        assert workspace.cursor_expected_field is None
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        assert pending.status == "abandoned_conflict"


def test_rollback_workspace_without_pending_sets_flow_one_and_routes_legacy(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    # The confirmation was consumed by a successful resume: the pending is
    # resolved and a later terminal execution of the same run carries the
    # accepted END head (provable supersession, see R02).
    _add_superseding_continuation(
        database_session_factory, fixture, fixture.saver
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "resolved"
        pending.resume_input_seq = 1
        session.commit()
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.flow_version_after == 1
    assert report.pending_outcomes == ()
    assert report.cursor_action == "none"
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.flow_version == 1


def test_rollback_blocks_resolved_pending_without_supersession(
    database_session_factory: sessionmaker[Session],
) -> None:
    """A resolved status alone never proves the retained interrupt was
    consumed: without a later accepted END head the rollback must block with
    zero writes (R02)."""
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "resolved"
        pending.resume_input_seq = 1
        session.commit()
    bridge = _bridge(database_session_factory, fixture.saver)

    with pytest.raises(RollbackBlockedError, match="neither live nor retired"):
        bridge.execute(workspace_token=fixture.token)

    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="resolved")


def test_rollback_accepts_resolved_pending_provably_superseded(
    database_session_factory: sessionmaker[Session],
) -> None:
    """A resolved pending whose interrupt is provably consumed by a later
    accepted END head does not block the flow-1 mapping (R02)."""
    fixture = _rollback_fixture(database_session_factory)
    _add_superseding_continuation(
        database_session_factory, fixture, fixture.saver
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "resolved"
        pending.resume_input_seq = 1
        session.commit()
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.flow_version_after == 1
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.flow_version == 1


# ---------------------------------------------------------------------------
# Blocking paths: zero flow / Cursor / pending modification
# ---------------------------------------------------------------------------
def test_rollback_unknown_kind_blocks_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    saver = InMemorySaver()
    other_run = uuid4()
    _craft_head(
        saver,
        thread_id=f"accesspilot:v1.3:{other_run}",
        head_id="ab" * 16,
        kind="mystery_kind",
        pending_input_id=fixture.pending_input_id,
        draft_revision=1,
        graph_run_id=other_run,
        workspace_ref=fixture.workspace_id,
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.accepted_checkpoint_id = "ab" * 16
        pending.graph_run_id = other_run
        pending.checkpoint_thread_id = f"accesspilot:v1.3:{other_run}"
        session.commit()
    bridge = _bridge(database_session_factory, saver)

    with pytest.raises(RollbackBlockedError, match="unknown pending kind"):
        bridge.execute(workspace_token=fixture.token)

    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")


def test_rollback_unreadable_head_blocks_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    # A different saver that never saw this workspace's head.
    bridge = _bridge(database_session_factory, InMemorySaver())

    with pytest.raises(RollbackBlockedError, match="unreadable"):
        bridge.execute(workspace_token=fixture.token)

    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")


def test_rollback_projection_task_mismatch_blocks_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    saver = InMemorySaver()
    other_run = uuid4()
    _craft_head(
        saver,
        thread_id=f"accesspilot:v1.3:{other_run}",
        head_id="cd" * 16,
        kind="confirmation",
        pending_input_id=uuid4(),  # different pending id than the row
        draft_revision=1,
        graph_run_id=other_run,
        workspace_ref=fixture.workspace_id,
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.accepted_checkpoint_id = "cd" * 16
        pending.graph_run_id = other_run
        pending.checkpoint_thread_id = f"accesspilot:v1.3:{other_run}"
        session.commit()
    bridge = _bridge(database_session_factory, saver)

    with pytest.raises(RollbackBlockedError, match="does not match"):
        bridge.execute(workspace_token=fixture.token)

    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")


def test_rollback_unfinished_execution_blocks_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        execution.status = "running"
        execution.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        execution.terminal_event_id = None
        session.commit()
    bridge = _bridge(database_session_factory, fixture.saver)

    with pytest.raises(RollbackBlockedError, match="unfinished"):
        bridge.execute(workspace_token=fixture.token)

    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")


def test_rollback_orphan_accepted_head_blocks_with_zero_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    # A second accepted head retains an interrupt task whose pending row does
    # not exist: an untombstoned orphan that no pending references.
    _add_orphan_accepted_head(
        database_session_factory, fixture, fixture.saver
    )
    bridge = _bridge(database_session_factory, fixture.saver)

    with pytest.raises(RollbackBlockedError, match="orphan"):
        bridge.execute(workspace_token=fixture.token)

    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")


def _add_orphan_accepted_head(
    factory: sessionmaker[Session],
    fixture: T35RollbackFixture,
    saver: InMemorySaver,
) -> UUID:
    """Point a second terminal execution at a crafted accepted head whose
    interrupt task references a pending_input_id that no pending row carries.
    (Pending rows are DB-immutable by design, so an orphan can only arise from
    the checkpoint side: a retained task without its projection row.)"""
    orphan_run = uuid4()
    orphan_head = "be" * 16
    orphan_pending_id = uuid4()
    _craft_head(
        saver,
        thread_id=f"accesspilot:v1.3:{orphan_run}",
        head_id=orphan_head,
        kind="confirmation",
        pending_input_id=orphan_pending_id,
        draft_revision=1,
        graph_run_id=orphan_run,
        workspace_ref=fixture.workspace_id,
    )
    with factory() as session:
        input_event = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="message.user",
            payload={"content": "x", "turn_id": f"turn-{uuid4()}"},
        )
        session.add(input_event)
        session.flush()
        terminal = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="message.completed",
            payload={
                "turn_id": f"turn-{uuid4()}",
                "message_id": f"msg-{uuid4()}",
                "content": "x",
                "intent": "help",
                "business_status": "answered",
                "draft_revision": 0,
            },
        )
        session.add(terminal)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=fixture.workspace_id,
                graph_run_id=orphan_run,
                checkpoint_thread_id=f"accesspilot:v1.3:{orphan_run}",
                input_seq=0,
                input_turn_id=f"turn-{uuid4()}",
                input_event_id=input_event.id,
                auth_session_ref=fixture.auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=None,
                status="waiting_input",
                checkpoint_ns="",
                accepted_checkpoint_id=orphan_head,
                terminal_event_id=terminal.id,
            )
        )
        session.commit()
    return orphan_head


def _assert_rollback_unchanged(
    factory: sessionmaker[Session],
    fixture: T35RollbackFixture,
    *,
    pending_status: str,
) -> None:
    """Zero flow / Cursor / pending modification on a blocked rollback."""
    with factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.flow_version == 2
        assert workspace.cursor_expected_field == "confirmation"
        assert workspace.cursor_consumed_at is None
        if pending_status != "deleted":
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.pending_input_id
                    == fixture.pending_input_id
                )
            )
            assert pending is not None
            assert pending.status == pending_status
            assert pending.retired_at is None
            assert pending.retirement_reason is None


def test_rollback_dry_run_only_reports_and_never_writes(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.plan(workspace_token=fixture.token)

    assert report.dry_run is True
    assert not report.blocked
    assert report.flow_version_before == 2
    assert report.flow_version_after == 1
    assert report.pending_outcomes[0].mapping == "abandoned_to_legacy"
    # Nothing was written.
    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")

    # A blocked dry run also reports instead of writing.
    bridge_empty = _bridge(database_session_factory, InMemorySaver())
    blocked_plan = bridge_empty.plan(workspace_token=fixture.token)
    assert blocked_plan.blocked is True
    assert blocked_plan.reasons
    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")

    # The same report shape becomes a real write on execute.
    executed = bridge.execute(workspace_token=fixture.token)
    assert executed.dry_run is False
    assert not executed.blocked
    assert executed.pending_outcomes[0].mapping == "abandoned_to_legacy"


# ---------------------------------------------------------------------------
# Publish / node-evolution preflight gate
# ---------------------------------------------------------------------------
def test_publish_preflight_passes_when_all_live_facts_are_zero(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    # Fully retired: the tombstone references the retained head, nothing live.
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "abandoned_to_legacy"
        pending.retired_at = datetime.now(UTC)
        pending.retirement_reason = "test rollback tombstone"
        session.commit()
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert report.passed
    assert report.reasons == ()
    assert report.live_pending_count == 0
    assert report.unfinished_execution_count == 0
    assert report.live_accepted_task_count == 0
    assert report.retained_task_count == 1


def test_publish_preflight_blocks_on_active_pending(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert not report.passed
    assert report.live_pending_count == 1
    assert report.live_accepted_task_count == 1


def test_publish_preflight_blocks_on_unfinished_execution(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        execution.status = "running"
        execution.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        execution.terminal_event_id = None
        session.commit()
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert not report.passed
    assert report.unfinished_execution_count == 1


def test_publish_preflight_blocks_on_orphan_accepted_head(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    _add_orphan_accepted_head(
        database_session_factory, fixture, fixture.saver
    )
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert not report.passed
    assert any("orphan" in reason for reason in report.reasons)
    # One live pending task plus the orphan accepted task are both live facts.
    assert report.live_accepted_task_count == 2


def test_publish_preflight_blocks_on_unreadable_pending_head(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    gate = PublishPreflightGate(
        database_session_factory, _reader(InMemorySaver())
    )

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert not report.passed
    assert any("unreadable" in reason for reason in report.reasons)


def test_publish_preflight_blocks_on_unknown_kind_and_projection_mismatch(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    saver = InMemorySaver()
    other_run = uuid4()
    _craft_head(
        saver,
        thread_id=f"accesspilot:v1.3:{other_run}",
        head_id="ef" * 16,
        kind="mystery_kind",
        pending_input_id=fixture.pending_input_id,
        draft_revision=1,
        graph_run_id=other_run,
        workspace_ref=fixture.workspace_id,
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.accepted_checkpoint_id = "ef" * 16
        pending.graph_run_id = other_run
        pending.checkpoint_thread_id = f"accesspilot:v1.3:{other_run}"
        session.commit()
    gate = PublishPreflightGate(database_session_factory, _reader(saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert not report.passed
    assert any("unknown pending kind" in reason for reason in report.reasons)


def test_publish_preflight_blocks_resolved_pending_without_supersession(
    database_session_factory: sessionmaker[Session],
) -> None:
    """A consumed confirmation keeps its historical interrupt task at the
    accepted head; status=resolved alone is never proof of consumption, so the
    gate must fail closed when no later accepted END head proves supersession
    (R02)."""
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "resolved"
        pending.resume_input_seq = 1
        session.commit()
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert not report.passed
    assert report.live_pending_count == 0
    assert report.live_accepted_task_count == 0
    assert any("neither live nor retired" in reason for reason in report.reasons)


def test_publish_preflight_accepts_resolved_pending_provably_superseded(
    database_session_factory: sessionmaker[Session],
) -> None:
    """The real T34 success shape: the resolved pending's interrupt task is
    provably consumed by the later terminal execution whose accepted END head
    retains no task.  The gate passes and reports the supersession fact (R02)."""
    fixture = _rollback_fixture(database_session_factory)
    _add_superseding_continuation(
        database_session_factory, fixture, fixture.saver
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "resolved"
        pending.resume_input_seq = 1
        session.commit()
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    report = gate.check(workspace_ids=[fixture.workspace_id])

    assert report.passed
    assert report.live_pending_count == 0
    assert report.live_accepted_task_count == 0
    assert report.superseded_task_count == 1
    assert report.retained_task_count == 0


def test_multiple_interrupt_tasks_on_one_head_block_preflight_and_rollback(
    database_session_factory: sessionmaker[Session],
) -> None:
    """The gate and the bridge must classify every retained interrupt at an
    accepted head: a first valid confirmation plus a second unknown/orphan
    interrupt on the same head fails closed with zero writes (R03)."""
    fixture = _rollback_fixture(database_session_factory)
    saver = InMemorySaver()
    other_run = uuid4()
    _craft_head(
        saver,
        thread_id=f"accesspilot:v1.3:{other_run}",
        head_id="dc" * 16,
        kind="confirmation",
        pending_input_id=fixture.pending_input_id,
        draft_revision=1,
        graph_run_id=other_run,
        workspace_ref=fixture.workspace_id,
        extra_interrupt_payloads=[
            {
                "kind": "mystery_kind",
                "pending_input_id": str(uuid4()),
                "draft_revision": 1,
            }
        ],
    )
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.accepted_checkpoint_id = "dc" * 16
        pending.graph_run_id = other_run
        pending.checkpoint_thread_id = f"accesspilot:v1.3:{other_run}"
        session.commit()

    gate = PublishPreflightGate(database_session_factory, _reader(saver))
    report = gate.check(workspace_ids=[fixture.workspace_id])
    assert not report.passed
    assert any("interrupt tasks" in reason for reason in report.reasons)

    bridge = _bridge(database_session_factory, saver)
    with pytest.raises(RollbackBlockedError, match="interrupt tasks"):
        bridge.execute(workspace_token=fixture.token)
    _assert_rollback_unchanged(database_session_factory, fixture, pending_status="active")


def test_publish_preflight_scoped_to_other_workspaces_ignores_them(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    other = _rollback_fixture(database_session_factory)
    gate = PublishPreflightGate(database_session_factory, _reader(fixture.saver))

    # Only the second workspace is in scope; the first workspace's live facts
    # and unreadable-in-this-saver heads must not affect the gate.
    report = gate.check(workspace_ids=[other.workspace_id])

    assert not report.passed
    assert report.live_pending_count == 1
    assert report.unfinished_execution_count == 0


# ---------------------------------------------------------------------------
# Tombstone heads are never resumed
# ---------------------------------------------------------------------------
def test_takeover_never_resumes_a_tombstoned_head(
    database_session_factory: sessionmaker[Session],
) -> None:
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "abandoned_to_legacy"
        pending.retired_at = datetime.now(UTC)
        pending.retirement_reason = "test tombstone"
        execution = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id
            )
        )
        assert execution is not None
        # Simulate a crashed executor that still holds an expired lease and
        # has never accepted a head itself.
        execution.status = "running"
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        execution.terminal_event_id = None
        execution.accepted_checkpoint_id = None
        execution.updated_at = datetime.now(UTC)
        session.commit()
    service = TurnExecutionService(database_session_factory)
    with service.advisory_lock(fixture.agent_thread_id) as lock:
        plan = service.takeover(
            workspace_token=fixture.token,
            graph_run_id=fixture.graph_run_id,
            input_seq=0,
            auth_session_ref=fixture.auth_session_id,
            actor_id="EMP-001",
            lock=lock,
            saver=fixture.saver,
        )

    # The tombstoned head must never become the rehydration source.
    assert plan.source == "input_event"
    assert plan.accepted_checkpoint_id is None


def test_takeover_historical_branch_never_resumes_a_tombstoned_head(
    database_session_factory: sessionmaker[Session],
) -> None:
    """R04: with two executions of the same run — a historical waiting_input
    execution whose accepted head was tombstoned, and an expired running
    execution with no head — the historical branch must not rehydrate the
    tombstone head either."""
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        pending = session.scalar(
            select(AgentPendingInputRecord).where(
                AgentPendingInputRecord.pending_input_id
                == fixture.pending_input_id
            )
        )
        assert pending is not None
        pending.status = "abandoned_to_legacy"
        pending.retired_at = datetime.now(UTC)
        pending.retirement_reason = "test tombstone"
        # The historical waiting_input execution keeps its accepted head,
        # exactly as a real rollback leaves it (R01 structure).
        historical = session.scalar(
            select(AgentTurnExecutionRecord).where(
                AgentTurnExecutionRecord.workspace_id == fixture.workspace_id,
                AgentTurnExecutionRecord.input_seq == 0,
            )
        )
        assert historical is not None
        assert historical.accepted_checkpoint_id == fixture.head_id
        # A crashed executor of the next input holds an expired lease and has
        # never accepted a head itself.
        input_event = WorkspaceEventRecord(
            workspace_id=fixture.workspace_id,
            event_type="message.user",
            payload={"content": "x", "turn_id": f"turn-{uuid4()}"},
        )
        session.add(input_event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=fixture.workspace_id,
                graph_run_id=fixture.graph_run_id,
                checkpoint_thread_id=fixture.thread_id,
                input_seq=1,
                input_turn_id=f"turn-{uuid4()}",
                input_event_id=input_event.id,
                auth_session_ref=fixture.auth_session_id,
                actor_id="EMP-001",
                engine="langgraph",
                attempt=1,
                lease_fence=1,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                status="running",
                checkpoint_ns="",
                accepted_checkpoint_id=None,
                terminal_event_id=None,
            )
        )
        session.commit()
    service = TurnExecutionService(database_session_factory)
    with service.advisory_lock(fixture.agent_thread_id) as lock:
        plan = service.takeover(
            workspace_token=fixture.token,
            graph_run_id=fixture.graph_run_id,
            input_seq=1,
            auth_session_ref=fixture.auth_session_id,
            actor_id="EMP-001",
            lock=lock,
            saver=fixture.saver,
        )

    # The historical execution's tombstoned head must not win the historical
    # branch; recovery falls back to the safe input event.
    assert plan.source == "input_event"
    assert plan.accepted_checkpoint_id is None


# ---------------------------------------------------------------------------
# Cursor kept/rewrite contract (R05)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "corrupt",
    [
        {"cursor_auth_session_id": str(uuid4())},
        {"cursor_actor_id": "EMP-999"},
        {"cursor_last_question_kind": "duration"},
    ],
)
def test_rollback_clean_mapping_rewrites_mismatched_cursor(
    database_session_factory: sessionmaker[Session],
    corrupt: dict[str, object],
) -> None:
    """R05: a Cursor may be kept only when actor, auth session, expected
    field, question kind and active state all match the verified pending.
    Any mismatch atomically rewrites the correct confirmation Cursor instead
    of leaving a wrong binding for the Legacy path."""
    fixture = _rollback_fixture(database_session_factory)
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        for key, value in corrupt.items():
            setattr(workspace, key, value)
        session.commit()
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.pending_outcomes[0].mapping == "abandoned_to_legacy"
    assert report.cursor_action == "created"
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.cursor_actor_id == "EMP-001"
        assert workspace.cursor_auth_session_id == str(fixture.auth_session_id)
        assert workspace.cursor_expected_field == "confirmation"
        assert workspace.cursor_last_question_kind == "confirmation"
        assert workspace.cursor_consumed_at is None


def test_rollback_clean_mapping_keeps_only_a_fully_matching_cursor(
    database_session_factory: sessionmaker[Session],
) -> None:
    """R05: the matching Cursor is kept untouched (same principal/session)."""
    fixture = _rollback_fixture(database_session_factory)
    bridge = _bridge(database_session_factory, fixture.saver)

    report = bridge.execute(workspace_token=fixture.token)

    assert not report.blocked
    assert report.cursor_action == "kept"
    with database_session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceRecord).where(WorkspaceRecord.id == fixture.workspace_id)
        )
        assert workspace is not None
        assert workspace.cursor_actor_id == "EMP-001"
        assert workspace.cursor_auth_session_id == str(fixture.auth_session_id)
        assert workspace.cursor_expected_field == "confirmation"
        assert workspace.cursor_last_question_kind == "confirmation"
        assert workspace.cursor_consumed_at is None
