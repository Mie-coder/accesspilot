"""add application-owned agent runtime facts

Revision ID: 20260817_0010
Revises: 20260812_0009
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0010"
down_revision: str | Sequence[str] | None = "20260812_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill private Workspace identity and add application ledgers."""

    # All three columns are added and backfilled in this revision's one
    # transaction.  gen_random_uuid() is evaluated per row, never once for the
    # whole table.
    op.add_column(
        "workspaces",
        sa.Column(
            "agent_thread_id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("flow_version", sa.Integer(), server_default="1", nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("lease_fence", sa.BigInteger(), server_default="0", nullable=True),
    )
    op.execute(
        "UPDATE workspaces "
        "SET agent_thread_id = gen_random_uuid(), flow_version = 1, lease_fence = 0 "
        "WHERE agent_thread_id IS NULL OR flow_version IS NULL OR lease_fence IS NULL"
    )
    op.alter_column("workspaces", "agent_thread_id", nullable=False)
    op.alter_column("workspaces", "flow_version", nullable=False)
    op.alter_column("workspaces", "lease_fence", nullable=False)
    op.create_unique_constraint(
        op.f("uq_workspaces_agent_thread_id"),
        "workspaces",
        ["agent_thread_id"],
    )
    op.create_unique_constraint(
        op.f("uq_workspaces_id"),
        "workspaces",
        ["id", "agent_thread_id"],
    )
    op.create_check_constraint(
        op.f("ck_workspaces_flow_version_valid"),
        "workspaces",
        "flow_version IN (1, 2)",
    )
    op.create_check_constraint(
        op.f("ck_workspaces_lease_fence_non_negative"),
        "workspaces",
        "lease_fence >= 0",
    )
    op.execute(
        """
        CREATE FUNCTION accesspilot_enforce_workspace_runtime_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.agent_thread_id IS DISTINCT FROM OLD.agent_thread_id THEN
                RAISE EXCEPTION 'agent_thread_id is immutable';
            END IF;
            IF NEW.lease_fence < OLD.lease_fence THEN
                RAISE EXCEPTION 'lease_fence cannot decrease';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workspaces_runtime_identity_immutable
        BEFORE UPDATE OF agent_thread_id, lease_fence ON workspaces
        FOR EACH ROW
        EXECUTE FUNCTION accesspilot_enforce_workspace_runtime_identity()
        """
    )

    op.create_unique_constraint(
        op.f("uq_auth_sessions_workspace_id"),
        "auth_sessions",
        ["workspace_id", "id", "employee_id"],
    )

    op.add_column(
        "workspace_events",
        sa.Column("event_key", sa.String(length=68), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_workspace_events_workspace_id"),
        "workspace_events",
        ["workspace_id", "id"],
    )
    op.create_check_constraint(
        op.f("ck_workspace_events_event_key_format"),
        "workspace_events",
        "event_key IS NULL OR event_key ~ '^evt_[0-9a-f]{64}$'",
    )
    op.create_index(
        "uq_workspace_events_workspace_event_key_not_null",
        "workspace_events",
        ["workspace_id", "event_key"],
        unique=True,
        postgresql_where=sa.text("event_key IS NOT NULL"),
    )

    op.create_table(
        "agent_turn_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("graph_run_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_thread_id", sa.String(length=100), nullable=False),
        sa.Column("input_seq", sa.Integer(), nullable=False),
        sa.Column("input_turn_id", sa.String(length=120), nullable=False),
        sa.Column("input_event_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_session_ref", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=30), nullable=False),
        sa.Column("engine", sa.String(length=20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_fence", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=200), server_default="", nullable=False),
        sa.Column("accepted_checkpoint_id", sa.String(length=200), nullable=True),
        sa.Column("terminal_event_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "checkpoint_ns = ''",
            name=op.f("ck_agent_turn_executions_root_checkpoint_namespace"),
        ),
        sa.CheckConstraint(
            "checkpoint_thread_id = 'accesspilot:v1.3:' || graph_run_id::text",
            name=op.f("ck_agent_turn_executions_checkpoint_thread_for_run"),
        ),
        sa.CheckConstraint(
            "accepted_checkpoint_id IS NULL OR length(accepted_checkpoint_id) > 0",
            name=op.f("ck_agent_turn_executions_accepted_checkpoint_id_non_empty"),
        ),
        sa.CheckConstraint(
            "engine IN ('legacy', 'langgraph')",
            name=op.f("ck_agent_turn_executions_engine_valid"),
        ),
        sa.CheckConstraint(
            "input_seq >= 0",
            name=op.f("ck_agent_turn_executions_input_seq_non_negative"),
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name=op.f("ck_agent_turn_executions_attempt_positive"),
        ),
        sa.CheckConstraint(
            "lease_fence >= 1",
            name=op.f("ck_agent_turn_executions_lease_fence_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_input', 'completed', "
            "'recoverable_error', 'interrupted')",
            name=op.f("ck_agent_turn_executions_status_valid"),
        ),
        sa.CheckConstraint(
            "((status = 'running' AND lease_expires_at IS NOT NULL "
            "AND terminal_event_id IS NULL) OR "
            "(status <> 'running' AND lease_expires_at IS NULL "
            "AND terminal_event_id IS NOT NULL))",
            name=op.f("ck_agent_turn_executions_lease_terminal_status_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "auth_session_ref", "actor_id"],
            [
                "auth_sessions.workspace_id",
                "auth_sessions.id",
                "auth_sessions.employee_id",
            ],
            name="fk_agent_turn_executions_auth_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "input_event_id"],
            ["workspace_events.workspace_id", "workspace_events.id"],
            name="fk_agent_turn_executions_input_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "terminal_event_id"],
            ["workspace_events.workspace_id", "workspace_events.id"],
            name="fk_agent_turn_executions_terminal_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_agent_turn_executions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_turn_executions")),
        sa.UniqueConstraint(
            "workspace_id",
            "graph_run_id",
            "input_seq",
            name=op.f("uq_agent_turn_executions_workspace_id"),
        ),
        sa.UniqueConstraint(
            "input_turn_id",
            name=op.f("uq_agent_turn_executions_input_turn_id"),
        ),
        sa.UniqueConstraint(
            "input_event_id",
            name=op.f("uq_agent_turn_executions_input_event_id"),
        ),
        sa.UniqueConstraint(
            "terminal_event_id",
            name=op.f("uq_agent_turn_executions_terminal_event_id"),
        ),
    )
    op.create_index(
        op.f("ix_agent_turn_executions_workspace_id"),
        "agent_turn_executions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "uq_agent_turn_executions_running_workspace",
        "agent_turn_executions",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "agent_pending_inputs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_thread_id", sa.Uuid(), nullable=False),
        sa.Column("graph_run_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_thread_id", sa.String(length=100), nullable=False),
        sa.Column("pending_input_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("auth_session_ref", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=30), nullable=False),
        sa.Column("engine", sa.String(length=20), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=200), server_default="", nullable=False),
        sa.Column("accepted_checkpoint_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("resume_input_seq", sa.Integer(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retirement_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind = 'confirmation'",
            name=op.f("ck_agent_pending_inputs_kind_valid"),
        ),
        sa.CheckConstraint(
            "draft_revision >= 0",
            name=op.f("ck_agent_pending_inputs_draft_revision_non_negative"),
        ),
        sa.CheckConstraint(
            "resume_input_seq IS NULL OR resume_input_seq >= 0",
            name=op.f("ck_agent_pending_inputs_resume_input_seq_non_negative"),
        ),
        sa.CheckConstraint(
            "engine IN ('legacy', 'langgraph')",
            name=op.f("ck_agent_pending_inputs_engine_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'resuming', 'resolved', "
            "'abandoned_to_legacy', 'abandoned_conflict')",
            name=op.f("ck_agent_pending_inputs_status_valid"),
        ),
        sa.CheckConstraint(
            "checkpoint_ns = ''",
            name=op.f("ck_agent_pending_inputs_root_checkpoint_namespace"),
        ),
        sa.CheckConstraint(
            "checkpoint_thread_id = 'accesspilot:v1.3:' || graph_run_id::text",
            name=op.f("ck_agent_pending_inputs_checkpoint_thread_for_run"),
        ),
        sa.CheckConstraint(
            "length(accepted_checkpoint_id) > 0",
            name=op.f("ck_agent_pending_inputs_accepted_checkpoint_id_non_empty"),
        ),
        sa.CheckConstraint(
            "((status IN ('active', 'abandoned_to_legacy', 'abandoned_conflict') "
            "AND resume_input_seq IS NULL) OR "
            "(status IN ('resuming', 'resolved') AND resume_input_seq IS NOT NULL))",
            name=op.f("ck_agent_pending_inputs_resume_sequence_status_consistent"),
        ),
        sa.CheckConstraint(
            "((status IN ('abandoned_to_legacy', 'abandoned_conflict') "
            "AND retired_at IS NOT NULL AND retirement_reason IS NOT NULL) OR "
            "(status NOT IN ('abandoned_to_legacy', 'abandoned_conflict') "
            "AND retired_at IS NULL AND retirement_reason IS NULL))",
            name=op.f("ck_agent_pending_inputs_retirement_status_consistent"),
        ),
        sa.CheckConstraint(
            "retirement_reason IS NULL OR length(btrim(retirement_reason)) > 0",
            name=op.f("ck_agent_pending_inputs_retirement_reason_non_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "auth_session_ref", "actor_id"],
            [
                "auth_sessions.workspace_id",
                "auth_sessions.id",
                "auth_sessions.employee_id",
            ],
            name="fk_agent_pending_inputs_auth_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_thread_id"],
            ["workspaces.id", "workspaces.agent_thread_id"],
            name="fk_agent_pending_inputs_workspace_thread",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_agent_pending_inputs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_pending_inputs")),
        sa.UniqueConstraint(
            "pending_input_id",
            name=op.f("uq_agent_pending_inputs_pending_input_id"),
        ),
    )
    op.create_index(
        op.f("ix_agent_pending_inputs_workspace_id"),
        "agent_pending_inputs",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "uq_agent_pending_inputs_live_workspace",
        "agent_pending_inputs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'resuming')"),
    )
    op.execute(
        """
        CREATE FUNCTION accesspilot_reject_retired_pending_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status IN ('abandoned_to_legacy', 'abandoned_conflict') THEN
                RAISE EXCEPTION 'retired pending input is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_pending_inputs_retired_immutable
        BEFORE UPDATE OR DELETE ON agent_pending_inputs
        FOR EACH ROW
        EXECUTE FUNCTION accesspilot_reject_retired_pending_mutation()
        """
    )

    op.create_table(
        "agent_step_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("graph_run_id", sa.Uuid(), nullable=False),
        sa.Column("input_seq", sa.Integer(), nullable=False),
        sa.Column("step_key", sa.String(length=120), nullable=False),
        sa.Column("operation_id", sa.String(length=67), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_reference", sa.String(length=255), nullable=True),
        sa.Column("committed_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "input_seq >= 0",
            name=op.f("ck_agent_step_executions_input_seq_non_negative"),
        ),
        sa.CheckConstraint(
            "committed_revision IS NULL OR committed_revision >= 0",
            name=op.f("ck_agent_step_executions_committed_revision_non_negative"),
        ),
        sa.CheckConstraint(
            "operation_id ~ '^op_[0-9a-f]{64}$'",
            name=op.f("ck_agent_step_executions_operation_id_format"),
        ),
        sa.CheckConstraint(
            "length(btrim(step_key)) > 0",
            name=op.f("ck_agent_step_executions_step_key_non_empty"),
        ),
        sa.CheckConstraint(
            "result_reference IS NULL OR length(btrim(result_reference)) > 0",
            name=op.f("ck_agent_step_executions_result_reference_non_empty"),
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'completed')",
            name=op.f("ck_agent_step_executions_status_valid"),
        ),
        sa.CheckConstraint(
            "((status = 'reserved' AND result_reference IS NULL "
            "AND committed_revision IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND (NULLIF(btrim(result_reference), '') IS NOT NULL "
            "OR committed_revision IS NOT NULL)))",
            name=op.f("ck_agent_step_executions_completion_facts_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "graph_run_id", "input_seq"],
            [
                "agent_turn_executions.workspace_id",
                "agent_turn_executions.graph_run_id",
                "agent_turn_executions.input_seq",
            ],
            name="fk_agent_step_executions_logical_input",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_agent_step_executions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_step_executions")),
        sa.UniqueConstraint(
            "workspace_id",
            "operation_id",
            name=op.f("uq_agent_step_executions_workspace_id"),
        ),
    )
    op.create_index(
        op.f("ix_agent_step_executions_workspace_id"),
        "agent_step_executions",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only AccessPilot-owned T28 facts."""

    op.drop_index(
        op.f("ix_agent_step_executions_workspace_id"),
        table_name="agent_step_executions",
    )
    op.drop_table("agent_step_executions")

    op.execute(
        "DROP TRIGGER trg_agent_pending_inputs_retired_immutable "
        "ON agent_pending_inputs"
    )
    op.execute("DROP FUNCTION accesspilot_reject_retired_pending_mutation()")
    op.drop_index(
        "uq_agent_pending_inputs_live_workspace",
        table_name="agent_pending_inputs",
        postgresql_where=sa.text("status IN ('active', 'resuming')"),
    )
    op.drop_index(
        op.f("ix_agent_pending_inputs_workspace_id"),
        table_name="agent_pending_inputs",
    )
    op.drop_table("agent_pending_inputs")

    op.drop_index(
        "uq_agent_turn_executions_running_workspace",
        table_name="agent_turn_executions",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_index(
        op.f("ix_agent_turn_executions_workspace_id"),
        table_name="agent_turn_executions",
    )
    op.drop_table("agent_turn_executions")

    op.drop_index(
        "uq_workspace_events_workspace_event_key_not_null",
        table_name="workspace_events",
        postgresql_where=sa.text("event_key IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("ck_workspace_events_event_key_format"),
        "workspace_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("uq_workspace_events_workspace_id"),
        "workspace_events",
        type_="unique",
    )
    op.drop_column("workspace_events", "event_key")

    op.drop_constraint(
        op.f("uq_auth_sessions_workspace_id"),
        "auth_sessions",
        type_="unique",
    )

    op.execute("DROP TRIGGER trg_workspaces_runtime_identity_immutable ON workspaces")
    op.execute("DROP FUNCTION accesspilot_enforce_workspace_runtime_identity()")
    op.drop_constraint(
        op.f("ck_workspaces_lease_fence_non_negative"),
        "workspaces",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_workspaces_flow_version_valid"),
        "workspaces",
        type_="check",
    )
    op.drop_constraint(
        op.f("uq_workspaces_id"),
        "workspaces",
        type_="unique",
    )
    op.drop_constraint(
        op.f("uq_workspaces_agent_thread_id"),
        "workspaces",
        type_="unique",
    )
    op.drop_column("workspaces", "lease_fence")
    op.drop_column("workspaces", "flow_version")
    op.drop_column("workspaces", "agent_thread_id")
