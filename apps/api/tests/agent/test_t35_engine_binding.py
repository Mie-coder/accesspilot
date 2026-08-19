"""T35 workspace sticky engine binding tests.

Covers the full allocation matrix, the stable ``agent_thread_id`` hash, the
fixed resolution order ``running execution.engine -> active/resuming
pending.engine -> Workspace flow_version`` with the fixed
409 ``ENGINE_BINDING_CONFLICT`` on any disagreement, client override
rejection, and the mixed-canary-0 gate that must hold before the T38/T40
entry gates exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.engine_binding import (
    EngineBindingConflictError,
    EngineBindingError,
    WorkspaceEngineResolver,
    allocate_flow_version,
    engine_to_flow,
    flow_to_engine,
    resolve_engine,
)
from accesspilot.config import Settings
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
from accesspilot.main import create_app
from accesspilot.streaming import DeterministicAnswerStreamModel
from support.auth import login_as


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _workspace_fixture(
    factory: sessionmaker[Session],
    *,
    flow_version: int,
    token: str | None = None,
) -> tuple[str, UUID, UUID, UUID]:
    """Create a workspace + valid auth session; return token, ids."""
    token = token or f"t35-binding-{uuid4()}"
    auth_session_id = uuid4()
    with factory() as session:
        seed_catalog(session)
        workspace = WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id="EMP-001",
            flow_version=flow_version,
            lease_fence=0,
            model_call_limit=20,
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        agent_thread_id = workspace.agent_thread_id
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
        session.commit()
    return token, workspace_id, agent_thread_id, auth_session_id


def _pending(
    factory: sessionmaker[Session],
    *,
    workspace_id: UUID,
    agent_thread_id: UUID,
    graph_run_id: UUID,
    engine: str,
    status: str,
    auth_session_id: UUID,
) -> None:
    with factory() as session:
        retired = status in ("abandoned_to_legacy", "abandoned_conflict")
        session.add(
            AgentPendingInputRecord(
                workspace_id=workspace_id,
                agent_thread_id=agent_thread_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                pending_input_id=uuid4(),
                kind="confirmation",
                draft_revision=0,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine=engine,
                checkpoint_ns="",
                accepted_checkpoint_id="1" * 32,
                status=status,
                resume_input_seq=1 if status in ("resuming", "resolved") else None,
                retired_at=datetime.now(UTC) if retired else None,
                retirement_reason=(
                    "test tombstone" if retired else None
                ),
            )
        )
        session.commit()


def _running_execution(
    factory: sessionmaker[Session],
    *,
    workspace_id: UUID,
    graph_run_id: UUID,
    engine: str,
    auth_session_id: UUID,
) -> None:
    with factory() as session:
        event = WorkspaceEventRecord(
            workspace_id=workspace_id,
            event_type="message.user",
            payload={"content": "x", "turn_id": f"turn-{uuid4()}"},
        )
        session.add(event)
        session.flush()
        session.add(
            AgentTurnExecutionRecord(
                workspace_id=workspace_id,
                graph_run_id=graph_run_id,
                checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
                input_seq=0,
                input_turn_id=f"turn-{uuid4()}",
                input_event_id=event.id,
                auth_session_ref=auth_session_id,
                actor_id="EMP-001",
                engine=engine,
                attempt=1,
                lease_fence=1,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                status="running",
                checkpoint_ns="",
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Allocation matrix
# ---------------------------------------------------------------------------
def test_allocate_flow_version_follows_mode_and_canary_matrix() -> None:
    thread_id = uuid4()
    assert allocate_flow_version(thread_id, mode="legacy", canary_percent=None) == 1
    assert allocate_flow_version(thread_id, mode="langgraph", canary_percent=None) == 2
    # mixed 0% means no flow 2 allocation at all (the T35 gate); 100% means all.
    assert (
        allocate_flow_version(thread_id, mode="mixed", canary_percent=0) == 1
    )
    assert (
        allocate_flow_version(thread_id, mode="mixed", canary_percent=100) == 2
    )
    # A middle canary must pick a flow deterministically.
    assert allocate_flow_version(thread_id, mode="mixed", canary_percent=50) in (1, 2)


def test_allocate_flow_version_mixed_bucket_is_stable_and_proportional() -> None:
    assigned: dict[UUID, int] = {}
    flow_two = 0
    sample = 3000
    for _ in range(sample):
        thread_id = uuid4()
        flow = allocate_flow_version(thread_id, mode="mixed", canary_percent=30)
        assert allocate_flow_version(thread_id, mode="mixed", canary_percent=30) == flow
        assigned[thread_id] = flow
        flow_two += flow == 2
    # The stable hash must be roughly uniform; a 30% canary must not silently
    # allocate everything or nothing.
    assert 0.20 * sample < flow_two < 0.40 * sample
    # The assignment is a pure function of the id: re-allocating with the same
    # id never drifts.
    for thread_id, flow in assigned.items():
        assert allocate_flow_version(thread_id, mode="mixed", canary_percent=30) == flow


def test_allocate_flow_version_rejects_invalid_configuration() -> None:
    thread_id = uuid4()
    with pytest.raises(EngineBindingError, match="required in mixed"):
        allocate_flow_version(thread_id, mode="mixed", canary_percent=None)
    with pytest.raises(EngineBindingError, match="between 0 and 100"):
        allocate_flow_version(thread_id, mode="mixed", canary_percent=-1)
    with pytest.raises(EngineBindingError, match="between 0 and 100"):
        allocate_flow_version(thread_id, mode="mixed", canary_percent=101)
    with pytest.raises(EngineBindingError, match="only valid in mixed"):
        allocate_flow_version(thread_id, mode="legacy", canary_percent=10)


def test_flow_and_engine_mappings_roundtrip() -> None:
    assert engine_to_flow("legacy") == 1
    assert engine_to_flow("langgraph") == 2
    assert flow_to_engine(1) == "legacy"
    assert flow_to_engine(2) == "langgraph"
    with pytest.raises(EngineBindingError):
        flow_to_engine(3)


# ---------------------------------------------------------------------------
# Resolution order and the fixed 409 conflict
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("running", "pending", "flow", "expected"),
    [
        (None, None, 1, "legacy"),
        (None, None, 2, "langgraph"),
        ("legacy", None, 1, "legacy"),
        ("langgraph", None, 2, "langgraph"),
        (None, "legacy", 1, "legacy"),
        (None, "langgraph", 2, "langgraph"),
        ("legacy", "legacy", 1, "legacy"),
        ("langgraph", "langgraph", 2, "langgraph"),
        ("legacy", "legacy", 2, None),  # conflict
        ("langgraph", "langgraph", 1, None),  # conflict
        ("legacy", None, 2, None),  # conflict
        ("langgraph", None, 1, None),  # conflict
        (None, "legacy", 2, None),  # conflict
        (None, "langgraph", 1, None),  # conflict
        ("legacy", "langgraph", 1, None),  # conflict
        ("langgraph", "legacy", 2, None),  # conflict
    ],
)
def test_resolve_engine_matrix_is_sticky_and_conflicts_fixed(
    running: str | None,
    pending: str | None,
    flow: int,
    expected: str | None,
) -> None:
    if expected is not None:
        assert (
            resolve_engine(
                workspace_flow_version=flow,
                running_execution_engine=running,
                pending_engine=pending,
            )
            == expected
        )
    else:
        with pytest.raises(EngineBindingConflictError) as captured:
            resolve_engine(
                workspace_flow_version=flow,
                running_execution_engine=running,
                pending_engine=pending,
            )
        assert captured.value.code == "ENGINE_BINDING_CONFLICT"


def test_resolve_engine_never_accepts_a_half_engine() -> None:
    with pytest.raises(EngineBindingError):
        resolve_engine(workspace_flow_version=2, running_execution_engine="shadow")


def test_workspace_resolver_follows_fixed_fact_order(
    database_session_factory: sessionmaker[Session],
) -> None:
    # flow alone decides.
    token, _wsid, _thread, auth = _workspace_fixture(
        database_session_factory, flow_version=1
    )
    resolver = WorkspaceEngineResolver(database_session_factory)
    assert resolver.resolve(workspace_token=token) == "legacy"

    # flow 2 alone resolves langgraph.
    token2, _wsid2, _thread2, _auth2 = _workspace_fixture(
        database_session_factory, flow_version=2
    )
    assert resolver.resolve(workspace_token=token2) == "langgraph"

    # active/resuming pending engine is authoritative and must agree.
    token3, wsid3, thread3, auth3 = _workspace_fixture(
        database_session_factory, flow_version=2
    )
    graph_run_id = uuid4()
    _pending(
        database_session_factory,
        workspace_id=wsid3,
        agent_thread_id=thread3,
        graph_run_id=graph_run_id,
        engine="langgraph",
        status="active",
        auth_session_id=auth3,
    )
    assert resolver.resolve(workspace_token=token3) == "langgraph"

    # A running execution that disagrees with the sticky flow is a fixed 409.
    token4, wsid4, _thread4, auth4 = _workspace_fixture(
        database_session_factory, flow_version=2
    )
    _running_execution(
        database_session_factory,
        workspace_id=wsid4,
        graph_run_id=uuid4(),
        engine="legacy",
        auth_session_id=auth4,
    )
    with pytest.raises(EngineBindingConflictError) as captured:
        resolver.resolve(workspace_token=token4)
    assert captured.value.code == "ENGINE_BINDING_CONFLICT"

    # An active pending that disagrees with the sticky flow is also a 409.
    token5, wsid5, thread5, auth5 = _workspace_fixture(
        database_session_factory, flow_version=1
    )
    _pending(
        database_session_factory,
        workspace_id=wsid5,
        agent_thread_id=thread5,
        graph_run_id=uuid4(),
        engine="langgraph",
        status="active",
        auth_session_id=auth5,
    )
    with pytest.raises(EngineBindingConflictError) as captured:
        resolver.resolve(workspace_token=token5)
    assert captured.value.code == "ENGINE_BINDING_CONFLICT"

    # Abandoned tombstones never participate in live resolution: the flow
    # alone resolves even though a retired langgraph pending still exists.
    token6, wsid6, thread6, auth6 = _workspace_fixture(
        database_session_factory, flow_version=2
    )
    _pending(
        database_session_factory,
        workspace_id=wsid6,
        agent_thread_id=thread6,
        graph_run_id=uuid4(),
        engine="langgraph",
        status="abandoned_to_legacy",
        auth_session_id=auth6,
    )
    assert resolver.resolve(workspace_token=token6) == "langgraph"


def test_workspace_resolver_unknown_workspace_fails_closed(
    database_session_factory: sessionmaker[Session],
) -> None:
    from accesspilot.workspaces import UnknownWorkspaceError

    resolver = WorkspaceEngineResolver(database_session_factory)
    with pytest.raises(UnknownWorkspaceError):
        resolver.resolve(workspace_token="no-such-token")


# ---------------------------------------------------------------------------
# Client override rejection and configuration gate
# ---------------------------------------------------------------------------
def test_client_cannot_override_engine_flow_or_thread(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        seed_catalog(session)
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
            answer_stream_model=DeterministicAnswerStreamModel(),
        )
    )
    login_as(client, session_factory=database_session_factory)

    for reserved in (
        {"engine": "langgraph"},
        {"flow_version": 2},
        {"agent_thread_id": str(uuid4())},
        {"thread_id": "accesspilot:v1.3:fake"},
        {"orchestrator_mode": "langgraph"},
    ):
        response = client.post(
            "/api/chat/messages",
            json={"content": "帮助", **reserved},
        )
        assert response.status_code == 422, reserved

    normal = client.post("/api/chat/messages", json={"content": "帮助"})
    assert normal.status_code == 200
    assert normal.json()["intent"] == "help"


def test_settings_keep_mixed_canary_disabled_until_entry_gates() -> None:
    # The T38/T40 entry gates do not exist yet: mixed must stay at 0 canary.
    with pytest.raises(ValidationError, match="must stay 0"):
        _settings(
            orchestrator_mode="mixed",
            langgraph_canary_percent=1,
            checkpoint_database_url="postgresql://runtime/db",
            langgraph_strict_msgpack=True,
        )
    with pytest.raises(ValidationError, match="must stay 0"):
        _settings(
            orchestrator_mode="mixed",
            langgraph_canary_percent=30,
            checkpoint_database_url="postgresql://runtime/db",
            langgraph_strict_msgpack=True,
        )
    # 0 is legal; missing and out-of-range remain illegal.
    assert (
        _settings(
            orchestrator_mode="mixed",
            langgraph_canary_percent=0,
            checkpoint_database_url="postgresql://runtime/db",
            langgraph_strict_msgpack=True,
        ).langgraph_canary_percent
        == 0
    )
    with pytest.raises(ValidationError, match="required in mixed"):
        _settings(
            orchestrator_mode="mixed",
            checkpoint_database_url="postgresql://runtime/db",
        )
    with pytest.raises(ValidationError, match="between 0 and 100"):
        _settings(
            orchestrator_mode="mixed",
            langgraph_canary_percent=101,
            checkpoint_database_url="postgresql://runtime/db",
        )


# ---------------------------------------------------------------------------
# Server-side sticky binding at workspace creation
# ---------------------------------------------------------------------------
def test_workspace_creation_binds_flow_server_side_in_the_create_transaction(
    database_session_factory: sessionmaker[Session],
) -> None:
    store = SqlAlchemyWorkspaceStore(
        database_session_factory,
        flow_allocator=lambda thread_id: allocate_flow_version(
            thread_id, mode="mixed", canary_percent=50
        ),
    )
    from accesspilot.domain.models import RequestDraft
    from accesspilot.workspaces import Workspace

    token = f"t35-create-{uuid4()}"
    workspace = Workspace(
        token=token,
        workspace_id=uuid4(),
        actor_id="EMP-001",
        draft=RequestDraft(employee_id="EMP-001"),
        draft_revision=0,
    )
    store.save(workspace)

    with database_session_factory() as session:
        record = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(token)
            )
        )
        assert record is not None
        assert record.flow_version == allocate_flow_version(
            record.agent_thread_id, mode="mixed", canary_percent=50
        )
        assert record.flow_version in (1, 2)
        # The thread identity is generated server-side inside the same row.
        assert record.agent_thread_id is not None

    # A second workspace gets an independent server-generated thread.
    token_b = f"t35-create-{uuid4()}"
    store.save(
        Workspace(
            token=token_b,
            workspace_id=uuid4(),
            actor_id="EMP-001",
            draft=RequestDraft(employee_id="EMP-001"),
            draft_revision=0,
        )
    )
    with database_session_factory() as session:
        threads = session.scalars(
            select(WorkspaceRecord.agent_thread_id).where(
                WorkspaceRecord.token_hash.in_(
                    (hash_workspace_token(token), hash_workspace_token(token_b))
                )
            )
        ).all()
        assert len(set(threads)) == 2


def test_default_store_allocates_legacy_flow_for_new_workspaces(
    database_session_factory: sessionmaker[Session],
) -> None:
    """Default production/local entry stays legacy: new workspaces are flow 1."""
    store = SqlAlchemyWorkspaceStore(database_session_factory)
    from accesspilot.domain.models import RequestDraft
    from accesspilot.workspaces import Workspace

    token = f"t35-default-{uuid4()}"
    store.save(
        Workspace(
            token=token,
            workspace_id=uuid4(),
            actor_id="EMP-001",
            draft=RequestDraft(employee_id="EMP-001"),
            draft_revision=0,
        )
    )
    with database_session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(WorkspaceRecord)
            .where(
                WorkspaceRecord.token_hash == hash_workspace_token(token),
                WorkspaceRecord.flow_version == 1,
            )
        )
        assert count == 1


def test_app_login_creates_flow_one_workspace_under_default_legacy_mode(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        seed_catalog(session)
    client = TestClient(
        create_app(
            settings=Settings(demo_mode_enabled=True),
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
            answer_stream_model=DeterministicAnswerStreamModel(),
        )
    )
    login = login_as(client, session_factory=database_session_factory)
    with database_session_factory() as session:
        record = session.scalar(
            select(WorkspaceRecord).where(
                WorkspaceRecord.token_hash == hash_workspace_token(login.session_token)
            )
        )
        assert record is not None
        assert record.flow_version == 1
