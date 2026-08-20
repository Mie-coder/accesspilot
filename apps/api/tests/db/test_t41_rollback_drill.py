"""T41 full disposable-PostgreSQL rollback and Legacy restart drill.

This is deliberately one end-to-end deployment story on a uniquely named
database: a flow-1 Workspace completes the four-role Case/Grant lifecycle, a
new flow-2 Workspace reaches a real PostgresSaver confirmation interrupt, the
deployment is stopped/drained, pending and checkpoint are reconciled, flow 2
is atomically retired to flow 1, and a fresh Legacy app reads both generations
of facts.  No existing database, checkpoint schema or business row is touched.
"""

from __future__ import annotations

import os
from uuid import UUID

import psycopg
import pytest
from apps.api.tests.api import test_t24_product_verification as t24
from apps.api.tests.db import test_t35_postgres as t35
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from accesspilot.agent.checkpoint import PostgresCheckpointRuntime
from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.agent.rollback import CheckpointTaskReader, PublishPreflightGate
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    ApprovalCaseRecord,
    AuthSessionRecord,
    WorkspaceEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.main import create_app
from support.auth import LoginResult, login_as

ORIGIN = "http://127.0.0.1:5173"


def _admin_conninfo() -> str:
    configured = t35._admin_url()
    if configured is None:
        pytest.skip("set the T35/T41 PostgreSQL admin URL")
    return make_url(configured).set(
        drivername="postgresql",
        database="postgres",
    ).render_as_string(hide_password=False)


def _resource_set(prefix: str) -> tuple[set[str], set[str]]:
    with psycopg.connect(_admin_conninfo(), autocommit=True) as admin:
        databases = {
            row[0]
            for row in admin.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (f"{prefix}%",),
            ).fetchall()
        }
        roles = {
            row[0]
            for row in admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                (f"{prefix}%",),
            ).fetchall()
        }
        return databases, roles


@pytest.mark.parametrize(
    "phase",
    [
        "migration_role",
        "runtime_role",
        "database",
        "extension",
        "settings",
        "alembic",
        "checkpoint",
    ],
)
def test_every_early_failure_restores_the_exact_resource_set(phase: str) -> None:
    prefix = "t41rb"
    before = _resource_set(prefix)
    with pytest.raises(
        RuntimeError,
        match=rf"injected isolated database failure after {phase}",
    ):
        with t35._isolated_checkpoint_database(
            resource_prefix=prefix,
            _fail_at=phase,
        ):
            raise AssertionError("unreachable")
    assert _resource_set(prefix) == before


def test_resource_prefix_is_validated_before_postgres_is_touched() -> None:
    before = _resource_set("t41rb")
    with pytest.raises(ValueError, match="resource_prefix"):
        with t35._isolated_checkpoint_database(resource_prefix="INVALID-"):
            raise AssertionError("unreachable")
    assert _resource_set("t41rb") == before


def _finish_four_role_case(
    clients: t24.RoleClients,
) -> tuple[str, str, str]:
    request_id, case_id, manager_step_id, owner_step_id = t24.submit_and_start_case(
        clients
    )
    manager = clients.manager.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": manager_step_id,
            "decision": "approve",
            "comment": "T41 虚构经理确认。",
        },
    )
    assert manager.status_code == 200, manager.text
    owner = clients.owner.post(
        f"/api/approval-cases/{case_id}/decisions",
        json={
            "approval_step_id": owner_step_id,
            "decision": "approve",
            "comment": "T41 虚构数据负责人确认。",
        },
    )
    assert owner.status_code == 200, owner.text
    provisioned = clients.admin.post(f"/api/requests/{request_id}/provision")
    assert provisioned.status_code == 200, provisioned.text
    return request_id, case_id, provisioned.json()["grant_id"]


def _resume_client(app: FastAPI, login: LoginResult) -> TestClient:
    client = TestClient(app)
    client.cookies.set("accesspilot_session", login.session_token)
    client.cookies.set("accesspilot_csrf", login.csrf_token)
    client.headers.update(
        {"Origin": ORIGIN, "X-CSRF-Token": login.csrf_token}
    )
    return client


def _langgraph_settings(base: Settings) -> Settings:
    return Settings(
        database_url=base.database_url,
        checkpoint_database_url=base.checkpoint_database_url,
        checkpoint_schema=base.checkpoint_schema,
        orchestrator_mode="langgraph",
        langgraph_strict_msgpack=True,
        deepseek_api_key=None,
        dashscope_api_key=None,
        web_origin=ORIGIN,
        _env_file=None,
    )


def test_full_drain_reconcile_downgrade_and_legacy_restart_preserves_facts() -> None:
    assert os.environ.get("LANGGRAPH_STRICT_MSGPACK") == "true"
    with t35._isolated_checkpoint_database(
        resource_prefix="t41rb"
    ) as base_settings:
        factory = build_session_factory(build_engine(base_settings.database_url))

        # Old Workspace: the public four-role product path creates a formal
        # request, packet, ordered approvals and exactly one simulated Grant.
        old_app, _advisory, _iam = t24.build_app(factory)
        old_clients = t24.login_four_roles(old_app)
        request_id, case_id, grant_id = _finish_four_role_case(old_clients)
        old_login = old_clients.requester_login
        for client in (
            old_clients.requester,
            old_clients.manager,
            old_clients.owner,
            old_clients.admin,
        ):
            client.close()

        # New Workspace: the product app uses the real official PostgresSaver
        # and deterministic no-credential adapters.  Closing TestClient is the
        # operator stop-accepting/drain boundary before reconciliation.
        langgraph_settings = _langgraph_settings(base_settings)
        graph_app = create_app(
            settings=langgraph_settings,
            session_factory=factory,
            embedding_model=DeterministicEmbeddingModel(),
        )
        with TestClient(graph_app) as graph_client:
            new_login = login_as(
                graph_client,
                "EMP-001",
                origin=ORIGIN,
                session_factory=factory,
            )
            pending_response = graph_client.post(
                "/api/chat/messages",
                json={
                    "content": (
                        "申请 insighthub.dashboard_view 14 天，"
                        "用于 T41 虚构回滚演练"
                    )
                },
            )
            assert pending_response.status_code == 200, pending_response.text
            assert (
                pending_response.json()["business_status"]
                == "awaiting_confirmation"
            )

        with factory() as session:
            new_workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash
                    == hash_workspace_token(new_login.session_token)
                )
            )
            assert new_workspace is not None
            new_workspace_id = new_workspace.id
            assert new_workspace.flow_version == 2
            assert new_workspace.cursor_expected_field == "confirmation"
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.workspace_id == new_workspace_id
                )
            )
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == new_workspace_id
                )
            )
            assert pending is not None and pending.status == "active"
            assert execution is not None and execution.status == "waiting_input"
            assert execution.accepted_checkpoint_id == pending.accepted_checkpoint_id
            exact_head = pending.accepted_checkpoint_id
            checkpoint_thread_id = pending.checkpoint_thread_id
            event_count_before = session.scalar(
                select(func.count())
                .select_from(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == new_workspace_id)
            )
            assert event_count_before and event_count_before > 0
            unfinished = session.scalar(
                select(func.count())
                .select_from(AgentTurnExecutionRecord)
                .where(AgentTurnExecutionRecord.terminal_event_id.is_(None))
            )
            assert unfinished == 0

        runtime = PostgresCheckpointRuntime(langgraph_settings)
        runtime.start()
        try:
            runtime.check_readiness()
            assert runtime.saver is not None
            reader = CheckpointTaskReader(runtime.saver)
            before = PublishPreflightGate(factory, reader).check()
            assert not before.passed
            assert before.live_pending_count == 1
            report = t35._bridge(factory, runtime).execute(
                workspace_token=new_login.session_token
            )
            assert not report.blocked
            assert report.flow_version_before == 2
            assert report.flow_version_after == 1
            assert report.pending_outcomes[0].mapping == "abandoned_to_legacy"
            assert (
                runtime.saver.get_tuple(
                    {
                        "configurable": {
                            "thread_id": checkpoint_thread_id,
                            "checkpoint_ns": "",
                            "checkpoint_id": exact_head,
                        }
                    }
                )
                is not None
            )
            after = PublishPreflightGate(factory, reader).check()
            assert after.passed
            assert after.live_pending_count == 0
            assert after.unfinished_execution_count == 0
            assert after.live_accepted_task_count == 0
            assert after.retained_task_count == 1
        finally:
            runtime.close()

        # Fresh application object with Legacy mode is the restart boundary.
        # Existing sessions, Workspaces and ACL-projected formal facts remain
        # readable; a subsequent chat turn is explicitly owned by Legacy.
        legacy_settings = Settings(
            database_url=base_settings.database_url,
            orchestrator_mode="legacy",
            deepseek_api_key=None,
            dashscope_api_key=None,
            web_origin=ORIGIN,
            _env_file=None,
        )
        restarted_app = create_app(
            settings=legacy_settings,
            session_factory=factory,
            embedding_model=DeterministicEmbeddingModel(),
        )
        with (
            _resume_client(restarted_app, old_login) as old_client,
            _resume_client(restarted_app, new_login) as new_client,
        ):
            old_detail = old_client.get(f"/api/requests/{request_id}")
            assert old_detail.status_code == 200, old_detail.text
            assert old_detail.json()["approval"]["approval_status"] == "approved"
            assert old_detail.json()["provisioning"]["grant_id"] == grant_id
            mine = old_client.get("/api/requests/mine")
            assert mine.status_code == 200
            assert request_id in {item["request_id"] for item in mine.json()["items"]}

            current_draft = new_client.get("/api/drafts/current")
            assert current_draft.status_code == 200
            assert current_draft.json()["draft"]["duration_days"] == 14
            replay = new_client.get("/api/events?follow=false")
            assert replay.status_code == 200
            assert "awaiting_confirmation" in replay.text
            legacy_turn = new_client.post(
                "/api/chat/messages", json={"content": "帮助"}
            )
            assert legacy_turn.status_code == 200, legacy_turn.text
            assert legacy_turn.json()["business_status"] == "answered"

        with factory() as session:
            old_auth = session.scalar(
                select(AuthSessionRecord).where(
                    AuthSessionRecord.token_hash
                    == hash_workspace_token(old_login.session_token)
                )
            )
            new_auth = session.scalar(
                select(AuthSessionRecord).where(
                    AuthSessionRecord.token_hash
                    == hash_workspace_token(new_login.session_token)
                )
            )
            assert old_auth is not None and new_auth is not None
            old_workspace = session.get(WorkspaceRecord, old_auth.workspace_id)
            new_workspace = session.get(WorkspaceRecord, new_auth.workspace_id)
            assert old_workspace is not None and old_workspace.flow_version == 1
            assert new_workspace is not None and new_workspace.flow_version == 1
            assert new_workspace.draft is not None
            assert new_workspace.draft["duration_days"] == 14
            pending = session.scalar(
                select(AgentPendingInputRecord).where(
                    AgentPendingInputRecord.workspace_id == new_workspace.id
                )
            )
            execution = session.scalar(
                select(AgentTurnExecutionRecord).where(
                    AgentTurnExecutionRecord.workspace_id == new_workspace.id
                )
            )
            assert pending is not None
            assert pending.status == "abandoned_to_legacy"
            assert pending.accepted_checkpoint_id == exact_head
            assert execution is not None
            assert execution.accepted_checkpoint_id == exact_head
            assert session.get(AccessRequestRecord, UUID(request_id)) is not None
            case = session.get(ApprovalCaseRecord, UUID(case_id))
            assert case is not None and case.approval_status == "approved"
            assert session.get(AccessGrantRecord, UUID(grant_id)) is not None
            event_count_after = session.scalar(
                select(func.count())
                .select_from(WorkspaceEventRecord)
                .where(WorkspaceEventRecord.workspace_id == new_workspace.id)
            )
            assert event_count_after and event_count_after > event_count_before
            execution_count = session.scalar(
                select(func.count())
                .select_from(AgentTurnExecutionRecord)
                .where(AgentTurnExecutionRecord.workspace_id == new_workspace.id)
            )
            # LegacyConversationOrchestrator owns no graph execution ledger;
            # a second row here would prove that restart dispatch ignored the
            # downgraded sticky flow and re-entered LangGraph.
            assert execution_count == 1
