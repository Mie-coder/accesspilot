"""T21 Decision Packet migration contract."""

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker


def test_0009_revision_adds_one_packet_per_request_without_destructive_cutover() -> None:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "20260812_0009_add_decision_packets.py"
    )
    source = path.read_text(encoding="utf-8")
    assert 'revision: str = "20260812_0009"' in source
    assert 'down_revision: str | Sequence[str] | None = "20260811_0008"' in source
    assert '"decision_packets"' in source
    assert '"request_id"' in source
    assert "unique=True" in source
    assert "frozen_content" in source
    assert "DROP TABLE access_requests" not in source.upper()


def test_decision_packets_table_has_unique_request_and_frozen_json(
    database_session_factory: sessionmaker[Session],
) -> None:
    with database_session_factory() as session:
        inspector = inspect(session.get_bind())
        columns = {
            column["name"]: column
            for column in inspector.get_columns("decision_packets")
        }
        assert {
            "id",
            "request_id",
            "packet_version",
            "generation_mode",
            "catalog_version",
            "frozen_content",
            "created_at",
        } <= set(columns)
        unique_sets = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("decision_packets")
        }
        assert ("request_id",) in unique_sets
        foreign_keys = inspector.get_foreign_keys("decision_packets")
        assert any(
            foreign_key["constrained_columns"] == ["request_id"]
            and foreign_key["referred_table"] == "access_requests"
            and foreign_key["referred_columns"] == ["id"]
            and foreign_key["options"].get("ondelete") == "CASCADE"
            for foreign_key in foreign_keys
        )
        checks = inspector.get_check_constraints("decision_packets")
        assert any(
            "generation_mode" in check["sqltext"]
            and all(
                mode in check["sqltext"]
                for mode in ("provider", "deterministic", "unavailable")
            )
            for check in checks
        )
