"""T30 strict-state proof against an isolated real PostgresSaver database."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import make_url

from accesspilot.agent.checkpoint import (
    FencedPostgresSaverAdapter,
    PostgresCheckpointRuntime,
    ServerExecutionContext,
    psycopg_connection_url,
)
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.production_graph import (
    GraphInput,
    UnsafeGraphStateUpdateError,
    build_production_graph,
)
from accesspilot.agent.routing import DeterministicIntentRouter
from accesspilot.agent.state import DraftPatch, GraphState, SafeToolResult
from accesspilot.auth import Principal
from accesspilot.checkpoint_init import run_official_checkpoint_setup
from accesspilot.config import Settings
from accesspilot.conversation import DeterministicStructuredReplyModel
from accesspilot.db.models import AgentTurnExecutionRecord
from accesspilot.events import ModelQuota
from accesspilot.tools.policies import PolicyService
from accesspilot.workspaces import InMemoryWorkspaceStore, Workspace, WorkspaceService

_ADMIN_URL_ENV = "ACCESSPILOT_T30_ADMIN_DATABASE_URL"
_FALLBACK_ADMIN_URL_ENV = "ACCESSPILOT_T29_ADMIN_DATABASE_URL"
_FORBIDDEN_KEYS = {
    "employee",
    "employee_id",
    "role",
    "roles",
    "confirmed",
    "raw_query",
    "policy_body",
    "vector",
    "provider_result",
    "sql",
    "exception",
    "reasoning",
    "session_factory",
    "workspace_service",
    "policy_service",
    "structured_reply_model",
    "intent_router",
    "principal",
    "current_turn_id",
    "current_fence",
    "workspace_token",
    "auth_session_id",
    "cookie",
    "csrf_token",
    "api_key",
}
_RUNTIME_CANARIES = {
    "runtime-turn-secret",
    "workspace-token-canary",
    "auth-session-canary",
    "cookie-canary",
    "csrf-canary",
    "sk-runtime-canary-12345678",
    "sk-input-canary-12345678",
}


@dataclass(frozen=True)
class _IsolatedCheckpointDatabase:
    admin_conninfo: str
    database: str
    migration_role: str
    runtime_role: str
    settings: Settings


@dataclass(frozen=True)
class _UnknownDataclass:
    value: str


class _UnknownModel(BaseModel):
    value: str


def _configured_admin_url() -> str | None:
    return os.getenv(_ADMIN_URL_ENV) or os.getenv(_FALLBACK_ADMIN_URL_ENV)


@contextmanager
def _isolated_checkpoint_database() -> Iterator[_IsolatedCheckpointDatabase]:
    configured = _configured_admin_url()
    if not configured:
        pytest.skip(f"set {_ADMIN_URL_ENV} to run the destructive-isolated T30 PostgreSQL proof")
    admin_url = make_url(configured).set(database="postgres")
    admin_conninfo = psycopg_connection_url(admin_url.render_as_string(hide_password=False))
    suffix = uuid4().hex[:12]
    database = f"t30_db_{suffix}"
    migration_role = f"t30_migration_{suffix}"
    runtime_role = f"t30_runtime_{suffix}"
    migration_password = f"m-{uuid4().hex}"
    runtime_password = f"r-{uuid4().hex}"
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(migration_role), sql.Literal(migration_password)
            )
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(runtime_role), sql.Literal(runtime_password)
            )
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(migration_role)
            )
        )
    migration_url = admin_url.set(
        username=migration_role,
        password=migration_password,
        database=database,
    ).render_as_string(hide_password=False)
    runtime_url = admin_url.set(
        username=runtime_role,
        password=runtime_password,
        database=database,
    ).render_as_string(hide_password=False)
    schema = f"t30_checkpoint_{suffix}"
    settings = Settings(
        database_url=migration_url,
        checkpoint_migration_database_url=migration_url,
        checkpoint_database_url=runtime_url,
        checkpoint_schema=schema,
        orchestrator_mode="mixed",
        langgraph_canary_percent=0,
        _env_file=None,
    )
    try:
        yield _IsolatedCheckpointDatabase(
            admin_conninfo=admin_conninfo,
            database=database,
            migration_role=migration_role,
            runtime_role=runtime_role,
            settings=settings,
        )
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_role)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migration_role)))


def _execution_context() -> ServerExecutionContext:
    graph_run_id = uuid4()
    record = AgentTurnExecutionRecord(
        id=uuid4(),
        workspace_id=uuid4(),
        graph_run_id=graph_run_id,
        checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
        input_seq=0,
        input_turn_id=str(uuid4()),
        input_event_id=1,
        auth_session_ref=uuid4(),
        actor_id="EMP-001",
        engine="langgraph",
        attempt=1,
        lease_fence=7,
        lease_expires_at=object(),
        status="running",
        checkpoint_ns="",
        accepted_checkpoint_id=None,
        terminal_event_id=None,
    )
    return ServerExecutionContext.from_record(record)


def _graph_input(context: ServerExecutionContext, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "flow_version": 2,
        "workspace_ref": context.workspace_id,
        "graph_run_id": context.graph_run_id,
        "input_seq": context.input_seq,
        "input_turn_id": context.input_turn_id,
        "safe_user_text": "请处理虚构权限业务，API_KEY=sk-input-canary-12345678",
        "input_kind": "new_input",
    }
    value.update(extra)
    return value


def _runtime_context(context: ServerExecutionContext) -> dict[str, object]:
    store = InMemoryWorkspaceStore()
    workspace = Workspace(
        token="workspace-token-canary",
        workspace_id=context.workspace_id,
        actor_id="EMP-001",
        auth_session_id="auth-session-canary",
    )
    store.save(workspace)
    return {
        "session_factory": lambda: nullcontext(object()),
        "workspace_service": WorkspaceService(store),
        "policy_service": PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        ),
        "structured_reply_model": DeterministicStructuredReplyModel(),
        "intent_router": DeterministicIntentRouter(),
        "principal": Principal(
            employee_id="EMP-001",
            name="林晓",
            department="数据平台部",
            roles=("analyst",),
        ),
        "current_turn_id": "runtime-turn-secret",
        "current_fence": 91,
        "workspace_token": "workspace-token-canary",
        "auth_session_id": "auth-session-canary",
        "cookie": "cookie-canary",
        "csrf_token": "csrf-canary",
        "api_key": "sk-runtime-canary-12345678",
    }


def _normalize_key(key: object) -> str:
    return str(key).strip().casefold().replace("-", "_").replace(" ", "_")


def _assert_safe_decoded(value: object, *, path: str = "root") -> None:
    if isinstance(value, BaseModel):
        _assert_safe_decoded(value.model_dump(mode="python"), path=path)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalize_key(key)
            assert normalized not in _FORBIDDEN_KEYS, f"forbidden key at {path}.{key}"
            _assert_safe_decoded(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_decoded(child, path=f"{path}[{index}]")
        return
    assert value is None or isinstance(value, (str, int, float, bool, bytes, UUID))
    if isinstance(value, str):
        for canary in _RUNTIME_CANARIES:
            assert canary not in value, f"secret canary at {path}"


def _checkpoint_rows(
    runtime: PostgresCheckpointRuntime,
    thread_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    assert runtime.pool is not None
    with runtime.pool.connection() as connection:
        checkpoints = connection.execute(
            "SELECT checkpoint, metadata FROM checkpoints WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        blobs = connection.execute(
            "SELECT channel, type, blob FROM checkpoint_blobs WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        writes = connection.execute(
            "SELECT channel, type, blob FROM checkpoint_writes WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
    return checkpoints, blobs, writes


def _assert_rows_are_safe(
    serializer: object,
    rows: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    checkpoints, blobs, writes = rows
    assert checkpoints
    assert blobs
    for row in checkpoints:
        _assert_safe_decoded(row["checkpoint"], path="checkpoints.checkpoint")
        _assert_safe_decoded(row["metadata"], path="checkpoints.metadata")
    for table_name, binary_rows in (
        ("checkpoint_blobs", blobs),
        ("checkpoint_writes", writes),
    ):
        for row in binary_rows:
            type_tag = row["type"]
            blob = row["blob"]
            assert type_tag != "pickle"
            if blob is None or type_tag == "empty":
                continue
            raw = bytes(blob)
            lowered = raw.lower()
            for canary in _RUNTIME_CANARIES:
                assert canary.encode().lower() not in lowered
            decoded = serializer.loads_typed((type_tag, raw))  # type: ignore[attr-defined]
            if row["channel"] == "__start__":
                assert isinstance(decoded, GraphInput)
            _assert_safe_decoded(decoded, path=f"{table_name}.{row['channel']}")


@pytest.mark.skipif(
    _configured_admin_url() is None,
    reason=f"set {_ADMIN_URL_ENV} for the isolated T30 PostgreSQL proof",
)
def test_t30_real_postgres_strict_checkpoint_scan_and_malicious_zero_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "accesspilot.agent.production_graph.get_model_quota",
        lambda *args, **kwargs: ModelQuota(
            used=0,
            limit=20,
            remaining=20,
            retry_consumed=0,
        ),
    )
    with _isolated_checkpoint_database() as isolated:
        run_official_checkpoint_setup(isolated.settings)
        runtime = PostgresCheckpointRuntime(isolated.settings)
        runtime.start()
        runtime.check_readiness()
        assert runtime.saver is not None
        try:
            context = _execution_context()
            invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(context)
            graph = build_production_graph(checkpointer=invocation)
            base_config = {
                "configurable": {
                    "thread_id": context.checkpoint_thread_id,
                    "checkpoint_ns": "",
                }
            }

            result = graph.invoke(
                _graph_input(context),
                config=base_config,
                context=_runtime_context(context),  # type: ignore[arg-type]
                durability="sync",
            )
            assert result.business_status == "answered"
            candidate = invocation.candidate
            assert candidate is not None
            compiled_saver = graph.compiled.checkpointer
            exact_tuple = compiled_saver.get_exact(candidate.locator)
            _assert_safe_decoded(exact_tuple.checkpoint, path="get_tuple.checkpoint")
            _assert_safe_decoded(exact_tuple.metadata, path="get_tuple.metadata")
            _assert_safe_decoded(
                exact_tuple.pending_writes,
                path="get_tuple.pending_writes",
            )
            snapshot = graph.compiled.get_state(candidate.locator.as_config())
            restored = GraphState.model_validate(snapshot.values)
            assert restored.safe_user_text == "请处理虚构权限业务，[已隐藏凭证]"
            _assert_rows_are_safe(
                compiled_saver.serde,
                _checkpoint_rows(runtime, context.checkpoint_thread_id),
            )

            # Exercise non-empty approved nested types through the real saver.
            # A scan of the default stub alone would only prove that None is safe.
            from accesspilot.agent import production_graph as production_graph_module

            def legal_nested_node_update(state, runtime_context):  # type: ignore[no-untyped-def]
                    del state, runtime_context
                    return {
                        "intent": "unknown",
                        "selected_route": "unknown",
                    "tool_name": "list_eligible_access",
                    "draft_patch": {
                        "entitlement_id": "insighthub.customer_export",
                        "duration_days": 30,
                        "justification": "核验虚构项目数据",
                    },
                    "safe_tool_result": {
                        "tool": "list_eligible_access",
                        "status": "success",
                        "entitlement_codes": ["insighthub.customer_export"],
                        "policy_codes": ["POL.ACCESS.001"],
                        "match_count": 1,
                        "summary": "命中 1 个虚构权限。",
                    },
                }

            monkeypatch.setattr(
                production_graph_module,
                "_route_intent",
                legal_nested_node_update,
            )
            nested_context = _execution_context()
            nested_invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(
                nested_context
            )
            nested_graph = build_production_graph(checkpointer=nested_invocation)
            nested_config = {
                "configurable": {
                    "thread_id": nested_context.checkpoint_thread_id,
                    "checkpoint_ns": "",
                }
            }
            nested_graph.invoke(
                _graph_input(nested_context),
                config=nested_config,
                context=_runtime_context(nested_context),  # type: ignore[arg-type]
                durability="sync",
            )
            nested_snapshot = nested_graph.compiled.get_state(
                nested_invocation.candidate.locator.as_config()
            )
            assert isinstance(nested_snapshot.values["draft_patch"], DraftPatch)
            assert isinstance(
                nested_snapshot.values["safe_tool_result"],
                SafeToolResult,
            )
            nested_rows = _checkpoint_rows(
                runtime,
                nested_context.checkpoint_thread_id,
            )
            _assert_rows_are_safe(nested_graph.compiled.checkpointer.serde, nested_rows)
            nested_decoded: dict[str, list[object]] = {
                "draft_patch": [],
                "safe_tool_result": [],
            }
            for row in [*nested_rows[1], *nested_rows[2]]:
                if row["channel"] not in nested_decoded or row["blob"] is None:
                    continue
                nested_decoded[row["channel"]].append(
                    nested_graph.compiled.checkpointer.serde.loads_typed(
                        (row["type"], bytes(row["blob"]))
                    )
                )
            assert any(isinstance(value, DraftPatch) for value in nested_decoded["draft_patch"])
            assert any(
                isinstance(value, SafeToolResult) for value in nested_decoded["safe_tool_result"]
            )

            malicious_context = _execution_context()
            malicious_invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(
                malicious_context
            )
            malicious_graph = build_production_graph(checkpointer=malicious_invocation)
            malicious_config = {
                "configurable": {
                    "thread_id": malicious_context.checkpoint_thread_id,
                    "checkpoint_ns": "",
                }
            }
            malicious_inputs = (
                {"draft_patch": {"employee_id": "EMP-001"}},
                {"safe_tool_result": {"policy_body": "raw-policy-canary"}},
                {"principal": {"roles": ["admin"]}},
                {"safe_user_text": {"set-canary"}},
                {"safe_user_text": b"pickle-canary"},
                {"safe_user_text": _UnknownDataclass("unknown-canary")},
                {"safe_user_text": _UnknownModel(value="unknown-canary")},
            )
            for malicious_update in malicious_inputs:
                with pytest.raises(ValidationError):
                    malicious_graph.invoke(
                        _graph_input(malicious_context, **malicious_update),
                        config=malicious_config,
                        context=_runtime_context(malicious_context),  # type: ignore[arg-type]
                        durability="sync",
                    )
            assert _checkpoint_rows(runtime, malicious_context.checkpoint_thread_id) == ([], [], [])

            # A future node implementation cannot rely on validation at the next
            # node: every registered node is wrapped by the pre-return state gate.
            # The valid input checkpoint may exist, but the malicious update and
            # its canary must never reach put_writes/checkpoint bytes.
            def malicious_node_update(state, runtime_context):  # type: ignore[no-untyped-def]
                del state, runtime_context
                return {
                    "safe_tool_result": {
                        "tool": "list_eligible_access",
                        "status": "success",
                        "entitlement_codes": [],
                        "policy_codes": [],
                        "match_count": 0,
                        "summary": "safe summary",
                        "provider_result": "node-provider-canary",
                    }
                }

            monkeypatch.setattr(
                production_graph_module,
                "_compose_safe_answer",
                malicious_node_update,
            )
            node_context = _execution_context()
            node_invocation = FencedPostgresSaverAdapter(runtime.saver).for_execution(node_context)
            node_graph = build_production_graph(checkpointer=node_invocation)
            node_config = {
                "configurable": {
                    "thread_id": node_context.checkpoint_thread_id,
                    "checkpoint_ns": "",
                }
            }
            with pytest.raises(UnsafeGraphStateUpdateError, match="invalid state update"):
                node_graph.invoke(
                    _graph_input(node_context),
                    config=node_config,
                    context=_runtime_context(node_context),  # type: ignore[arg-type]
                    durability="sync",
                )
            node_rows = _checkpoint_rows(runtime, node_context.checkpoint_thread_id)
            assert node_rows[0]
            _assert_rows_are_safe(node_graph.compiled.checkpointer.serde, node_rows)
            for table_rows in node_rows:
                for row in table_rows:
                    for value in row.values():
                        if isinstance(value, (bytes, bytearray, memoryview)):
                            assert b"node-provider-canary" not in bytes(value)
                        else:
                            assert "node-provider-canary" not in str(value)
        finally:
            runtime.close()
