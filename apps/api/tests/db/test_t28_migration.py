"""T28 Alembic revision contract."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "20260817_0010_add_agent_runtime_facts.py"
)


def test_0010_revision_owns_only_application_schema() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260817_0010"' in source
    assert 'down_revision: str | Sequence[str] | None = "20260812_0009"' in source
    for required in (
        "agent_thread_id",
        "flow_version",
        "lease_fence",
        "agent_turn_executions",
        "agent_pending_inputs",
        "agent_step_executions",
        "event_key",
    ):
        assert required in source
    assert "UPDATE workspaces" in source
    assert "gen_random_uuid()" in source
    assert "event_key IS NOT NULL" in source
    assert "DROP SCHEMA" not in source.upper()
    for checkpointer_table in (
        '"checkpoints"',
        '"checkpoint_blobs"',
        '"checkpoint_writes"',
        '"checkpoint_migrations"',
    ):
        assert checkpointer_table not in source
