"""T29 checkpoint configuration, lifecycle, and fenced adapter contracts."""

from __future__ import annotations

import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.checkpoint import (
    CandidateCheckpointRejected,
    CheckpointLocator,
    CheckpointUnavailableError,
    ExactCheckpointRequired,
    FencedPostgresSaverAdapter,
    ServerExecutionContext,
)
from accesspilot.config import Settings
from accesspilot.db.models import AgentTurnExecutionRecord
from accesspilot.main import create_app


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=create_engine("sqlite://"), expire_on_commit=False)


def test_orchestrator_configuration_is_strict() -> None:
    assert _settings().orchestrator_mode == "legacy"
    assert _settings().langgraph_canary_percent is None

    with pytest.raises(ValidationError, match="orchestrator_mode"):
        _settings(orchestrator_mode="shadow")
    with pytest.raises(ValidationError, match="required in mixed"):
        _settings(orchestrator_mode="mixed", checkpoint_database_url="postgresql://runtime/db")
    with pytest.raises(ValidationError, match="between 0 and 100"):
        _settings(
            orchestrator_mode="mixed",
            langgraph_canary_percent=101,
            checkpoint_database_url="postgresql://runtime/db",
        )
    with pytest.raises(ValidationError, match="only valid in mixed"):
        _settings(orchestrator_mode="legacy", langgraph_canary_percent=0)
    with pytest.raises(ValidationError, match="checkpoint_database_url"):
        _settings(orchestrator_mode="langgraph")
    with pytest.raises(ValidationError, match="independent checkpoint schema"):
        _settings(checkpoint_schema="public")


class _FakeCheckpointRuntime:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.start_calls = 0
        self.readiness_calls = 0
        self.close_calls = 0
        self.setup_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def check_readiness(self) -> None:
        self.readiness_calls += 1
        if self.unavailable:
            raise CheckpointUnavailableError("checkpoint unavailable")

    def close(self) -> None:
        self.close_calls += 1


def test_legacy_lifecycle_never_constructs_checkpoint_runtime() -> None:
    constructed: list[_FakeCheckpointRuntime] = []

    def factory(settings: Settings) -> _FakeCheckpointRuntime:
        del settings
        runtime = _FakeCheckpointRuntime()
        constructed.append(runtime)
        return runtime

    app = create_app(
        settings=_settings(orchestrator_mode="legacy"),
        session_factory=_session_factory(),
        checkpoint_runtime_factory=factory,
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200

    assert constructed == []


def test_enabled_lifecycle_reuses_one_runtime_and_closes_it() -> None:
    runtimes: list[_FakeCheckpointRuntime] = []

    def factory(settings: Settings) -> _FakeCheckpointRuntime:
        assert settings.orchestrator_mode == "mixed"
        runtime = _FakeCheckpointRuntime()
        runtimes.append(runtime)
        return runtime

    settings = _settings(
        orchestrator_mode="mixed",
        langgraph_canary_percent=0,
        checkpoint_database_url="postgresql://runtime/db",
    )
    app = create_app(
        settings=settings,
        session_factory=_session_factory(),
        checkpoint_runtime_factory=factory,
    )
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/ready").status_code == 200
        assert runtimes[0].start_calls == 1
        assert runtimes[0].readiness_calls == 2
        assert runtimes[0].setup_calls == 0
        assert runtimes[0].close_calls == 0

    assert runtimes[0].close_calls == 1


def test_enabled_checkpoint_failure_only_fails_readiness() -> None:
    runtime = _FakeCheckpointRuntime(unavailable=True)
    app = create_app(
        settings=_settings(
            orchestrator_mode="langgraph",
            checkpoint_database_url="postgresql://runtime/db",
        ),
        session_factory=_session_factory(),
        checkpoint_runtime_factory=lambda settings: runtime,
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Checkpoint 暂不可用"}

    assert runtime.start_calls == 1
    assert runtime.close_calls == 1


def _execution_record(*, accepted_checkpoint_id: str | None = None) -> AgentTurnExecutionRecord:
    graph_run_id = uuid4()
    return AgentTurnExecutionRecord(
        id=uuid4(),
        workspace_id=uuid4(),
        graph_run_id=graph_run_id,
        checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
        input_seq=2,
        input_turn_id=f"turn-{uuid4()}",
        input_event_id=101,
        auth_session_ref=uuid4(),
        actor_id="EMP-001",
        engine="langgraph",
        attempt=1,
        lease_fence=7,
        lease_expires_at=SimpleNamespace(),
        status="running",
        checkpoint_ns="",
        accepted_checkpoint_id=accepted_checkpoint_id,
        terminal_event_id=None,
    )


class _FakeSaver:
    def __init__(self) -> None:
        self.get_configs: list[dict[str, object]] = []
        self.put_configs: list[dict[str, object]] = []
        self.write_configs: list[dict[str, object]] = []
        self.version_calls: list[tuple[object | None, object]] = []
        self.tuples: dict[str, object] = {}

    def get_tuple(self, config):  # type: ignore[no-untyped-def]
        self.get_configs.append(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        return self.tuples.get(checkpoint_id)

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[no-untyped-def]
        del metadata, new_versions
        self.put_configs.append(config)
        saved = {
            "configurable": {
                "thread_id": config["configurable"]["thread_id"],
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint["id"],
            }
        }
        self.tuples[checkpoint["id"]] = object()
        return saved

    def put_writes(self, config, writes, task_id, task_path=""):  # type: ignore[no-untyped-def]
        del writes, task_id, task_path
        self.write_configs.append(config)

    def get_next_version(self, current, channel):  # type: ignore[no-untyped-def]
        self.version_calls.append((current, channel))
        return "00000000000000000000000000000008.0.1234567890123456"


def test_fenced_adapter_delegates_official_channel_version_generation() -> None:
    context = ServerExecutionContext.from_record(_execution_record())
    saver = _FakeSaver()
    invocation = FencedPostgresSaverAdapter(saver).for_execution(context)

    assert invocation.get_next_version(
        "00000000000000000000000000000007.0.9876543210987654", None
    ) == "00000000000000000000000000000008.0.1234567890123456"
    assert saver.version_calls == [
        ("00000000000000000000000000000007.0.9876543210987654", None)
    ]


def test_fenced_adapter_preserves_postgres_saver_string_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    monkeypatch.setattr(
        "langgraph.checkpoint.postgres.base.random.random", lambda: 0.125
    )
    official = PostgresSaver(None)  # type: ignore[arg-type]
    current = "00000000000000000000000000000007.0.9876543210987654"
    expected = official.get_next_version(current, None)
    invocation = FencedPostgresSaverAdapter(official).for_execution(
        ServerExecutionContext.from_record(_execution_record())
    )

    actual = invocation.get_next_version(current, None)
    assert actual == expected
    assert re.fullmatch(r"[0-9]{32}\.[0-9]+\.[0-9]+", actual)


def test_fenced_adapter_never_reads_implicit_latest_and_put_is_candidate_only() -> None:
    record = _execution_record()
    context = ServerExecutionContext.from_record(record)
    saver = _FakeSaver()
    invocation = FencedPostgresSaverAdapter(saver).for_execution(context)

    assert invocation.get_tuple(
        {"configurable": {"thread_id": record.checkpoint_thread_id, "checkpoint_ns": ""}}
    ) is None
    assert saver.get_configs == []

    saved = invocation.put(
        {"configurable": {"thread_id": record.checkpoint_thread_id, "checkpoint_ns": ""}},
        {"id": "candidate-1"},
        {},
        {},
    )
    assert saved["configurable"]["checkpoint_id"] == "candidate-1"
    assert invocation.candidate is not None
    assert invocation.candidate.locator.checkpoint_id == "candidate-1"

    with pytest.raises(ExactCheckpointRequired, match="checkpoint_id"):
        invocation.get_tuple(
            {"configurable": {"thread_id": record.checkpoint_thread_id, "checkpoint_ns": ""}}
        )


def test_exact_reads_are_scoped_to_server_execution_and_candidate_validation() -> None:
    record = _execution_record(accepted_checkpoint_id="accepted-1")
    context = ServerExecutionContext.from_record(record)
    saver = _FakeSaver()
    saver.tuples["accepted-1"] = object()
    invocation = FencedPostgresSaverAdapter(saver).for_execution(context)
    exact = CheckpointLocator(
        checkpoint_thread_id=record.checkpoint_thread_id,
        checkpoint_ns="",
        checkpoint_id="accepted-1",
    )

    assert invocation.get_exact(exact) is saver.tuples["accepted-1"]
    with pytest.raises(ExactCheckpointRequired, match="accepted head"):
        invocation.get_exact(
            CheckpointLocator(record.checkpoint_thread_id, "", "orphan-latest")
        )
    with pytest.raises(ValueError, match="root graph"):
        CheckpointLocator(record.checkpoint_thread_id, "child", "head")

    invocation.put(exact.as_config(), {"id": "candidate-2"}, {}, {})
    candidate = invocation.candidate
    assert candidate is not None
    with pytest.raises(CandidateCheckpointRejected, match="stopped"):
        invocation.verify_candidate(
            candidate,
            graph_stopped=False,
            graph_state_reader=lambda config: object(),
            state_validator=lambda state: True,
        )
    with pytest.raises(CandidateCheckpointRejected, match="state"):
        invocation.verify_candidate(
            candidate,
            graph_stopped=True,
            graph_state_reader=lambda config: SimpleNamespace(config=config),
            state_validator=lambda state: False,
        )


def test_put_lineage_must_start_at_accepted_head_and_cannot_escape() -> None:
    record = _execution_record(accepted_checkpoint_id="accepted-1")
    context = ServerExecutionContext.from_record(record)
    saver = _FakeSaver()
    invocation = FencedPostgresSaverAdapter(saver).for_execution(context)

    with pytest.raises(ExactCheckpointRequired, match="accepted checkpoint head"):
        invocation.put(
            CheckpointLocator(record.checkpoint_thread_id, "", "stale-parent").as_config(),
            {"id": "candidate-stale"},
            {},
            {},
        )

    invocation.put(
        CheckpointLocator(record.checkpoint_thread_id, "", "accepted-1").as_config(),
        {"id": "candidate-1"},
        {},
        {},
    )
    with pytest.raises(ExactCheckpointRequired, match="immediate candidate"):
        invocation.put(
            CheckpointLocator(record.checkpoint_thread_id, "", "accepted-1").as_config(),
            {"id": "candidate-branch"},
            {},
            {},
        )
    with pytest.raises(ExactCheckpointRequired, match="history/list"):
        list(invocation.list(None))
    with pytest.raises(ExactCheckpointRequired, match="deletion"):
        invocation.delete_thread(record.checkpoint_thread_id)
