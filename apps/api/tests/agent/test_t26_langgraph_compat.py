import importlib.util
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest


def _load_probe_module():
    script_path = Path(__file__).resolve().parents[4] / "scripts" / "t26_langgraph_compat.py"
    spec = importlib.util.spec_from_file_location(
        "accesspilot_t26_langgraph_compat_test_module", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe_module()
ExactHeadMissing = PROBE.ExactHeadMissing
assert_strict_msgpack = PROBE.assert_strict_msgpack
build_checkpoint_thread_id = PROBE.build_checkpoint_thread_id
invoke_from_exact_head = PROBE.invoke_from_exact_head
root_checkpoint_config = PROBE.root_checkpoint_config
sqlalchemy_to_psycopg_url = PROBE.sqlalchemy_to_psycopg_url


EXPECTED_VERSIONS = dict(PROBE.EXPECTED_VERSIONS)


def test_candidate_dependency_matrix_is_exact() -> None:
    resolved: dict[str, str] = {}
    for package in EXPECTED_VERSIONS:
        try:
            resolved[package] = version(package)
        except PackageNotFoundError:
            resolved[package] = "missing"

    assert resolved == EXPECTED_VERSIONS


def test_each_graph_run_gets_a_distinct_server_thread() -> None:
    first = build_checkpoint_thread_id("v1.3", "run-001")
    second = build_checkpoint_thread_id("v1.3", "run-002")

    assert first == "accesspilot:v1.3:run-001"
    assert second == "accesspilot:v1.3:run-002"
    assert first != second

    with pytest.raises(ValueError, match="safe token"):
        build_checkpoint_thread_id("v1.3", "run:client-controlled")


def test_root_namespace_is_empty_and_nonempty_namespace_fails_fast() -> None:
    config = root_checkpoint_config("accesspilot:v1.3:run-001", checkpoint_id="checkpoint-001")

    assert config == {
        "configurable": {
            "thread_id": "accesspilot:v1.3:run-001",
            "checkpoint_ns": "",
            "checkpoint_id": "checkpoint-001",
        }
    }
    with pytest.raises(ValueError, match="root graph"):
        root_checkpoint_config("accesspilot:v1.3:run-001", checkpoint_ns="business-run")


def test_sqlalchemy_url_conversion_preserves_encoded_credentials() -> None:
    converted = sqlalchemy_to_psycopg_url(
        "postgresql+psycopg://fictional:p%40ss@127.0.0.1:55432/accesspilot_test?sslmode=disable"
    )

    assert converted == (
        "postgresql://fictional:p%40ss@127.0.0.1:55432/accesspilot_test?sslmode=disable"
    )
    assert "+psycopg" not in converted

    with pytest.raises(ValueError, match="PostgreSQL psycopg"):
        sqlalchemy_to_psycopg_url("sqlite:///tmp/accesspilot.db")


class _FakeSaver:
    def __init__(self, checkpoint: object | None) -> None:
        self.checkpoint = checkpoint
        self.configs: list[dict[str, object]] = []

    def get_tuple(self, config: dict[str, object]) -> object | None:
        self.configs.append(config)
        return self.checkpoint


class _FakeGraph:
    def __init__(self) -> None:
        self.invocations = 0

    def invoke(
        self,
        graph_input: object,
        config: dict[str, object],
        **kwargs: object,
    ) -> object:
        self.invocations += 1
        return {"graph_input": graph_input, "config": config, "kwargs": kwargs}


def test_missing_exact_head_fails_before_graph_invocation() -> None:
    saver = _FakeSaver(None)
    graph = _FakeGraph()
    config = root_checkpoint_config("accesspilot:v1.3:run-001", checkpoint_id="missing-head")

    with pytest.raises(ExactHeadMissing, match="does not exist"):
        invoke_from_exact_head(saver, graph, config, "approved")

    assert graph.invocations == 0
    assert saver.configs == [config]


def test_existing_exact_head_allows_one_graph_invocation() -> None:
    saver = _FakeSaver(object())
    graph = _FakeGraph()
    config = root_checkpoint_config("accesspilot:v1.3:run-001", checkpoint_id="existing-head")

    result = invoke_from_exact_head(saver, graph, config, "approved", durability="sync")

    assert graph.invocations == 1
    assert result == {
        "graph_input": "approved",
        "config": config,
        "kwargs": {"durability": "sync"},
    }


def test_strict_msgpack_is_active_before_serializer_import() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scripts.t26_langgraph_compat import assert_strict_msgpack; "
            "assert_strict_msgpack()",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true"},
    )

    assert completed.returncode == 0, completed.stderr


def test_strict_msgpack_rejects_late_or_missing_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)

    with pytest.raises(RuntimeError, match="before process start"):
        assert_strict_msgpack()


@pytest.mark.skipif(
    "ACCESSPILOT_T26_DATABASE_URL" not in os.environ,
    reason="set ACCESSPILOT_T26_DATABASE_URL for the destructive-isolated PostgreSQL probe",
)
def test_real_postgres_interrupt_resume_contract() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/t26_langgraph_compat.py", "--integration"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true"},
    )

    assert completed.returncode == 0, completed.stderr
    assert '"status": "passed"' in completed.stdout
