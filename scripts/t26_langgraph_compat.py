#!/usr/bin/env python3
"""Isolated T26 compatibility probe for LangGraph's synchronous Postgres saver.

The probe writes only to a uniquely named temporary PostgreSQL schema and drops
that schema before it exits. It never prints the database URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from importlib.metadata import PackageNotFoundError, version
from typing import Any, NotRequired, Protocol, TypedDict
from uuid import uuid4

EXPECTED_VERSIONS = {
    "langgraph": "1.2.11",
    "langgraph-checkpoint": "4.2.0",
    "langgraph-checkpoint-postgres": "3.1.2",
    "psycopg": "3.3.4",
    "psycopg-pool": "3.3.1",
}

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
_PROBE_SCHEMA = re.compile(r"^t26_checkpoint_[0-9a-f]{12}$")


class ExactHeadMissing(RuntimeError):
    """Raised before graph invocation when an exact checkpoint cannot be read."""


class CandidatePromotionRejected(RuntimeError):
    """Raised when a candidate has not passed stop-and-state validation."""


class SaverLike(Protocol):
    def get_tuple(self, config: dict[str, Any]) -> object | None: ...


class GraphLike(Protocol):
    def invoke(
        self,
        graph_input: object,
        config: dict[str, Any],
        **kwargs: object,
    ) -> object: ...


class ProbeState(TypedDict):
    request: str
    decision: NotRequired[str]
    status: NotRequired[str]


def resolved_versions() -> dict[str, str]:
    """Return the approved package matrix without failing on missing packages."""

    resolved: dict[str, str] = {}
    for package in EXPECTED_VERSIONS:
        try:
            resolved[package] = version(package)
        except PackageNotFoundError:
            resolved[package] = "missing"
    return resolved


def assert_candidate_versions() -> None:
    """Fail when the executing interpreter does not use the approved matrix."""

    resolved = resolved_versions()
    if resolved != EXPECTED_VERSIONS:
        raise RuntimeError(
            "T26 dependency matrix mismatch: "
            + ", ".join(
                f"{name}={resolved[name]} (expected {expected})"
                for name, expected in EXPECTED_VERSIONS.items()
                if resolved[name] != expected
            )
        )


def assert_strict_msgpack() -> None:
    """Prove strict deserialization was enabled before LangGraph was imported."""

    configured = os.getenv("LANGGRAPH_STRICT_MSGPACK", "").lower()
    if configured not in {"1", "true", "yes"}:
        raise RuntimeError("LANGGRAPH_STRICT_MSGPACK must be true before process start")

    from langgraph.checkpoint.serde import _msgpack
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serializer = JsonPlusSerializer()
    if not _msgpack.STRICT_MSGPACK_ENABLED:
        raise RuntimeError("LangGraph imported before strict msgpack became active")
    if serializer._allowed_msgpack_modules is not None:
        raise RuntimeError("default serializer is not restricted to the safe allowlist")


def build_checkpoint_thread_id(graph_version: str, graph_run_id: str) -> str:
    """Build the server-side, per-run checkpoint thread identifier."""

    for label, value in (
        ("graph_version", graph_version),
        ("graph_run_id", graph_run_id),
    ):
        if not value or not _SAFE_TOKEN.fullmatch(value):
            raise ValueError(f"{label} must be a non-empty safe token")
    return f"accesspilot:{graph_version}:{graph_run_id}"


def root_checkpoint_config(
    checkpoint_thread_id: str,
    *,
    checkpoint_id: str | None = None,
    checkpoint_ns: str = "",
) -> dict[str, Any]:
    """Create a root-graph config and reject unsupported business namespaces."""

    if checkpoint_ns != "":
        raise ValueError("the v1.3 root graph requires checkpoint_ns='' ")
    if not checkpoint_thread_id.startswith("accesspilot:"):
        raise ValueError("checkpoint_thread_id must be server generated")

    configurable: dict[str, str] = {
        "thread_id": checkpoint_thread_id,
        "checkpoint_ns": "",
    }
    if checkpoint_id is not None:
        if not checkpoint_id:
            raise ValueError("checkpoint_id cannot be empty")
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def sqlalchemy_to_psycopg_url(database_url: str) -> str:
    """Convert a SQLAlchemy psycopg URL without decoding credentials by hand."""

    from sqlalchemy.engine import make_url

    parsed = make_url(database_url)
    if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("T26 requires a PostgreSQL psycopg URL")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def require_exact_head(saver: SaverLike, config: dict[str, Any]) -> object:
    """Fail closed unless the full root thread/ns/id locator exists."""

    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise ExactHeadMissing("checkpoint config is missing configurable")
    if configurable.get("checkpoint_ns") != "":
        raise ExactHeadMissing("exact root head requires checkpoint_ns='' ")
    if not configurable.get("thread_id") or not configurable.get("checkpoint_id"):
        raise ExactHeadMissing("exact head requires thread_id and checkpoint_id")

    checkpoint = saver.get_tuple(config)
    if checkpoint is None:
        raise ExactHeadMissing("exact checkpoint head does not exist for this run")
    return checkpoint


def invoke_from_exact_head(
    saver: SaverLike,
    graph: GraphLike,
    config: dict[str, Any],
    graph_input: object,
    **invoke_kwargs: object,
) -> object:
    """Guard graph invocation with an exact-head read."""

    require_exact_head(saver, config)
    return graph.invoke(graph_input, config, **invoke_kwargs)


def validate_interrupted_snapshot(
    snapshot: object, *, expected_config: dict[str, Any]
) -> None:
    """Require the exact candidate state to contain a dynamic interrupt."""

    snapshot_config = getattr(snapshot, "config", {})
    expected_locator = expected_config["configurable"]
    actual_locator = snapshot_config.get("configurable", {})
    for key in ("thread_id", "checkpoint_ns", "checkpoint_id"):
        if actual_locator.get(key) != expected_locator.get(key):
            raise CandidatePromotionRejected(
                f"exact candidate locator mismatch for {key}"
            )
    tasks = getattr(snapshot, "tasks", ())
    if not tasks or not any(getattr(task, "interrupts", ()) for task in tasks):
        raise CandidatePromotionRejected("exact candidate is not an interrupt state")


def promote_candidate(
    conn: Any,
    *,
    run_id: str,
    expected_fence: int,
    checkpoint_thread_id: str,
    checkpoint_id: str,
    graph_stopped: bool,
    exact_state_validated: bool,
) -> bool:
    """Spike the fenced candidate-to-accepted-head promotion preconditions."""

    if not graph_stopped or not exact_state_validated:
        raise CandidatePromotionRejected(
            "candidate requires a stopped graph and validated exact state"
        )
    result = conn.execute(
        """
        UPDATE t26_head_acceptance
        SET checkpoint_thread_id = %s,
            checkpoint_ns = '',
            accepted_checkpoint_id = %s
        WHERE run_id = %s
          AND fence = %s
          AND accepted_checkpoint_id IS NULL
        """,
        (checkpoint_thread_id, checkpoint_id, run_id, expected_fence),
    )
    return result.rowcount == 1


def _await_confirmation(state: ProbeState) -> dict[str, str]:
    from langgraph.types import interrupt

    decision = interrupt(
        {
            "kind": "confirmation",
            "request": state["request"],
            "allowed_decisions": ["approved", "rejected"],
        }
    )
    return {"decision": str(decision), "status": "completed"}


def _compile_probe_graph(saver: object) -> object:
    from langgraph.constants import END, START
    from langgraph.graph import StateGraph

    builder = StateGraph(ProbeState)
    builder.add_node("await_confirmation", _await_confirmation)
    builder.add_edge(START, "await_confirmation")
    builder.add_edge("await_confirmation", END)
    return builder.compile(checkpointer=saver)


def _validate_probe_schema(schema: str) -> None:
    if not _PROBE_SCHEMA.fullmatch(schema):
        raise ValueError("invalid T26 temporary schema")


def _pool_kwargs(schema: str) -> dict[str, object]:
    _validate_probe_schema(schema)
    from psycopg.rows import dict_row

    return {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "options": f"-c search_path={schema},public",
    }


def _assert_pool_contract(pool: object, schema: str) -> None:
    from psycopg.rows import dict_row

    with (
        pool.connection() as first,  # type: ignore[attr-defined]
        pool.connection() as second,  # type: ignore[attr-defined]
    ):
        for conn in (first, second):
            if not conn.autocommit or conn.prepare_threshold != 0:
                raise RuntimeError("pool connection lacks saver transaction settings")
            if conn.row_factory is not dict_row:
                raise RuntimeError("pool connection does not use dict_row")
            row = conn.execute("SHOW search_path").fetchone()
            if not isinstance(row, dict) or schema not in row["search_path"]:
                raise RuntimeError("checkpoint schema is absent from search_path")


def _recording_saver_type() -> type:
    from langgraph.checkpoint.postgres import PostgresSaver

    class RecordingPostgresSaver(PostgresSaver):
        def __init__(self, conn: object) -> None:
            super().__init__(conn)  # type: ignore[arg-type]
            self.trace: list[dict[str, object]] = []
            self._trace_lock = threading.Lock()

        def put(
            self,
            config: dict[str, Any],
            checkpoint: dict[str, Any],
            metadata: dict[str, Any],
            new_versions: dict[str, Any],
        ) -> dict[str, Any]:
            saved = super().put(config, checkpoint, metadata, new_versions)
            with self._trace_lock:
                self.trace.append(
                    {
                        "operation": "put",
                        "checkpoint_id": saved["configurable"]["checkpoint_id"],
                    }
                )
            return saved

        def put_writes(
            self,
            config: dict[str, Any],
            writes: list[tuple[str, Any]],
            task_id: str,
            task_path: str = "",
        ) -> None:
            super().put_writes(config, writes, task_id, task_path)
            with self._trace_lock:
                self.trace.append(
                    {
                        "operation": "put_writes",
                        "checkpoint_id": config["configurable"]["checkpoint_id"],
                        "channels": [channel for channel, _ in writes],
                    }
                )

    return RecordingPostgresSaver


def _find_interrupt_candidate(trace: list[dict[str, object]]) -> str:
    for interrupt_index, entry in enumerate(trace):
        channels = entry.get("channels", [])
        if entry.get("operation") != "put_writes" or "__interrupt__" not in channels:
            continue
        candidate = str(entry["checkpoint_id"])
        if not any(
            previous.get("operation") == "put" and previous.get("checkpoint_id") == candidate
            for previous in trace[:interrupt_index]
        ):
            raise RuntimeError("interrupt put_writes was not preceded by put(head)")
        return candidate
    raise RuntimeError("probe did not observe interrupt put_writes")


def _database_url_from_environment() -> str:
    database_url = os.getenv("ACCESSPILOT_T26_DATABASE_URL")
    if not database_url:
        raise RuntimeError("ACCESSPILOT_T26_DATABASE_URL is required")
    return sqlalchemy_to_psycopg_url(database_url)


def _run_resume(schema: str, checkpoint_thread_id: str, checkpoint_id: str) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.types import Command
    from psycopg_pool import ConnectionPool

    _validate_probe_schema(schema)
    conninfo = _database_url_from_environment()
    with ConnectionPool(
        conninfo,
        min_size=1,
        max_size=2,
        kwargs=_pool_kwargs(schema),
        open=True,
    ) as pool:
        pool.wait()
        _assert_pool_contract(pool, schema)
        saver = PostgresSaver(pool)
        graph = _compile_probe_graph(saver)
        exact_config = root_checkpoint_config(checkpoint_thread_id, checkpoint_id=checkpoint_id)
        require_exact_head(saver, exact_config)
        validate_interrupted_snapshot(
            graph.get_state(exact_config), expected_config=exact_config
        )
        result = invoke_from_exact_head(
            saver,
            graph,
            exact_config,
            Command(resume="approved"),
            durability="sync",
        )
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise RuntimeError("cross-process resume did not reach END")

        latest = saver.get_tuple(root_checkpoint_config(checkpoint_thread_id))
        if latest is None:
            raise RuntimeError("resume did not persist a latest head")
        print(
            json.dumps(
                {
                    "phase": "resume",
                    "latest_checkpoint_id": latest.config["configurable"]["checkpoint_id"],
                }
            )
        )


class _NeverInvokedGraph:
    def __init__(self) -> None:
        self.invocations = 0

    def invoke(
        self,
        graph_input: object,
        config: dict[str, Any],
        **kwargs: object,
    ) -> object:
        self.invocations += 1
        raise AssertionError("graph must not be invoked for a missing exact head")


def _run_integration() -> None:
    assert_candidate_versions()
    assert_strict_msgpack()

    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    conninfo = _database_url_from_environment()
    schema = f"t26_checkpoint_{uuid4().hex[:12]}"
    _validate_probe_schema(schema)
    graph_run_a = f"run-{uuid4().hex}"
    graph_run_b = f"run-{uuid4().hex}"
    thread_a = build_checkpoint_thread_id("v1.3", graph_run_a)
    thread_b = build_checkpoint_thread_id("v1.3", graph_run_b)
    if thread_a == thread_b:
        raise RuntimeError("two graph runs shared a checkpoint thread")

    with psycopg.connect(conninfo, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    try:
        with ConnectionPool(
            conninfo,
            min_size=1,
            max_size=3,
            kwargs=_pool_kwargs(schema),
            open=True,
        ) as pool:
            pool.wait()
            _assert_pool_contract(pool, schema)
            saver_type = _recording_saver_type()
            saver = saver_type(pool)
            saver.setup()
            saver.setup()

            with pool.connection() as conn:
                tables = conn.execute(
                    """
                    SELECT array_agg(c.relname ORDER BY c.relname) AS names
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s
                      AND c.relname IN ('checkpoints', 'checkpoint_writes')
                    """,
                    (schema,),
                ).fetchone()
                if not tables or tables["names"] != [
                    "checkpoint_writes",
                    "checkpoints",
                ]:
                    raise RuntimeError("PostgresSaver setup did not use the custom schema")
                conn.execute(
                    """
                    CREATE TABLE t26_head_acceptance (
                        run_id text PRIMARY KEY,
                        fence bigint NOT NULL,
                        checkpoint_thread_id text,
                        checkpoint_ns text,
                        accepted_checkpoint_id text
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO t26_head_acceptance (run_id, fence) VALUES (%s, %s)",
                    (graph_run_a, 7),
                )

            graph = _compile_probe_graph(saver)
            base_a = root_checkpoint_config(thread_a)
            interrupted_result = graph.invoke(
                {"request": "fictional analytics read access"},
                base_a,
                durability="sync",
            )
            if (
                not isinstance(interrupted_result, dict)
                or "__interrupt__" not in interrupted_result
            ):
                raise RuntimeError("graph did not stop at the expected interrupt")
            graph_stopped = True
            interrupt_candidate = _find_interrupt_candidate(saver.trace)
            exact_a = root_checkpoint_config(thread_a, checkpoint_id=interrupt_candidate)
            require_exact_head(saver, exact_a)
            validate_interrupted_snapshot(
                graph.get_state(exact_a), expected_config=exact_a
            )
            exact_state_validated = True

            missing_config = root_checkpoint_config(
                thread_a, checkpoint_id="00000000-0000-0000-0000-000000000000"
            )
            never_invoked = _NeverInvokedGraph()
            try:
                invoke_from_exact_head(saver, never_invoked, missing_config, object())
            except ExactHeadMissing:
                pass
            else:
                raise RuntimeError("missing exact head did not fail closed")
            if never_invoked.invocations:
                raise RuntimeError("missing exact head reached graph invocation")

            wrong_run_config = root_checkpoint_config(thread_b, checkpoint_id=interrupt_candidate)
            try:
                require_exact_head(saver, wrong_run_config)
            except ExactHeadMissing:
                pass
            else:
                raise RuntimeError("wrong-run exact head did not fail closed")

            with pool.connection() as conn:
                for stopped, validated in ((False, True), (True, False)):
                    try:
                        promote_candidate(
                            conn,
                            run_id=graph_run_a,
                            expected_fence=7,
                            checkpoint_thread_id=thread_a,
                            checkpoint_id=interrupt_candidate,
                            graph_stopped=stopped,
                            exact_state_validated=validated,
                        )
                    except CandidatePromotionRejected:
                        pass
                    else:
                        raise RuntimeError("unvalidated candidate was promoted")
                if promote_candidate(
                    conn,
                    run_id=graph_run_a,
                    expected_fence=6,
                    checkpoint_thread_id=thread_a,
                    checkpoint_id=interrupt_candidate,
                    graph_stopped=graph_stopped,
                    exact_state_validated=exact_state_validated,
                ):
                    raise RuntimeError("stale fence promoted a candidate")
                if not promote_candidate(
                    conn,
                    run_id=graph_run_a,
                    expected_fence=7,
                    checkpoint_thread_id=thread_a,
                    checkpoint_id=interrupt_candidate,
                    graph_stopped=True,
                    exact_state_validated=True,
                ):
                    raise RuntimeError("validated candidate was not promoted")

            child = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--resume",
                    "--schema",
                    schema,
                    "--thread-id",
                    thread_a,
                    "--checkpoint-id",
                    interrupt_candidate,
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true"},
            )
            if child.returncode != 0:
                raise RuntimeError("cross-process resume failed: " + child.stderr.strip())

            old_exact = saver.get_tuple(exact_a)
            latest_a = saver.get_tuple(base_a)
            if old_exact is None or latest_a is None:
                raise RuntimeError("old exact or latest head disappeared after resume")
            latest_a_id = latest_a.config["configurable"]["checkpoint_id"]
            if latest_a_id == interrupt_candidate:
                raise RuntimeError("resume did not create a newer checkpoint head")
            if old_exact.config["configurable"]["checkpoint_id"] != interrupt_candidate:
                raise RuntimeError("exact old-head lookup silently selected latest")

            base_b = root_checkpoint_config(thread_b)
            graph.invoke(
                {"request": "fictional dashboard read access"},
                base_b,
                durability="sync",
            )
            latest_b = saver.get_tuple(base_b)
            if latest_b is None:
                raise RuntimeError("second graph run did not persist a head")
            if latest_b.config["configurable"]["thread_id"] == thread_a:
                raise RuntimeError("two graph runs were not isolated")

        print(
            json.dumps(
                {
                    "status": "passed",
                    "versions": EXPECTED_VERSIONS,
                    "strict_msgpack": True,
                    "setup_calls": 2,
                    "cross_process_resume": True,
                    "exact_old_head": True,
                    "per_run_isolation": True,
                    "fenced_promotion": True,
                },
                sort_keys=True,
            )
        )
    finally:
        with psycopg.connect(conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--integration", action="store_true")
    modes.add_argument("--resume", action="store_true")
    parser.add_argument("--schema")
    parser.add_argument("--thread-id")
    parser.add_argument("--checkpoint-id")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    assert_candidate_versions()
    assert_strict_msgpack()
    if args.integration:
        _run_integration()
        return
    if not args.schema or not args.thread_id or not args.checkpoint_id:
        raise SystemExit("--resume requires --schema, --thread-id and --checkpoint-id")
    _run_resume(args.schema, args.thread_id, args.checkpoint_id)


if __name__ == "__main__":
    main()
