"""T21 Decision Packet API and lifecycle contract tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.config import Settings
from accesspilot.db.models import (
    AccessRequestRecord,
    ApprovalCaseRecord,
    AuditEventRecord,
    DecisionPacketRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.decision_packets import (
    DecisionAdvisory,
    DecisionAdvisoryContext,
    generate_decision_packet,
)
from accesspilot.main import create_app
from accesspilot.rag.policies import index_policy_embeddings
from support.auth import login_as

ORIGIN = "http://127.0.0.1:5173"


class RecordingAdvisoryModel:
    generation_mode = "deterministic"

    def __init__(
        self,
        advisory: DecisionAdvisory | None = None,
    ) -> None:
        self.calls: list[DecisionAdvisoryContext] = []
        self._lock = Lock()
        self._advisory = advisory or DecisionAdvisory(
            assessment="blocked",
            summary="规则建议关注高风险数据导出的最小期限。",
            unknowns=["业务说明需由人工复核。"],
            recommendations=["人工核对申请理由与使用期限。"],
            citations=["POL-003"],
        )

    def review(self, context: DecisionAdvisoryContext) -> DecisionAdvisory:
        with self._lock:
            self.calls.append(context)
        return self._advisory


class FailingAdvisoryModel:
    generation_mode = "provider"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def review(self, context: DecisionAdvisoryContext) -> DecisionAdvisory:
        del context
        self.calls += 1
        raise self.error


def build_client(
    database_session_factory: sessionmaker[Session],
    *,
    advisory_model: object | None = None,
) -> TestClient:
    embedding_model = DeterministicEmbeddingModel()
    with database_session_factory() as session:
        seed_catalog(session)
        index_policy_embeddings(session, embedding_model)
    return TestClient(
        create_app(
            settings=Settings(web_origin=ORIGIN, deepseek_api_key=None),
            store=SqlAlchemyWorkspaceStore(database_session_factory),
            session_factory=database_session_factory,
            embedding_model=embedding_model,
            decision_advisory_model=advisory_model,
        )
    )


def submit_request(
    client: TestClient,
    database_session_factory: sessionmaker[Session],
) -> str:
    login_as(client, "EMP-001", session_factory=database_session_factory)
    preview = client.post(
        "/api/drafts/preview",
        json={
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 14,
            "justification": (
                "核验项目数据，联系 linxiao@example.test，"
                "不应把 EMP-001 或 sk-private-t21 发给模型"
            ),
            "confirmed": True,
        },
    )
    assert preview.status_code == 200, preview.text
    submitted = client.post("/api/requests")
    assert submitted.status_code == 201, submitted.text
    return submitted.json()["request_id"]


def test_requester_creates_one_frozen_packet_with_four_sources_and_fixed_route(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    requester = build_client(
        database_session_factory,
        advisory_model=model,
    )
    request_id = submit_request(requester, database_session_factory)

    created = requester.post(f"/api/requests/{request_id}/decision-packet")
    repeated = requester.post(f"/api/requests/{request_id}/decision-packet")

    assert created.status_code == 201, created.text
    assert repeated.status_code == 200, repeated.text
    packet = created.json()
    assert repeated.json()["packet_id"] == packet["packet_id"]
    assert packet["request_id"] == request_id
    assert packet["packet_version"] == "v1"
    assert packet["catalog_version"] == "fictional-catalog-v1"
    assert packet["generation_mode"] == "deterministic"
    assert packet["advisory"]["assessment"] == "blocked"
    assert [
        (step["approver_id"], step["approver_role"])
        for step in packet["fixed_route"]
    ] == [("EMP-002", "manager"), ("EMP-003", "data_owner")]
    assert {item["source_kind"] for item in packet["items"]} == {
        "verified_fact",
        "policy_evidence",
        "user_claim",
        "advisory",
    }
    assert all(item["source_version"] for item in packet["items"])
    assert len(model.calls) == 1
    outbound = model.calls[0].model_dump_json()
    assert "EMP-001" not in outbound
    assert "insighthub.customer_export" not in outbound
    assert "linxiao@example.test" not in outbound
    assert "sk-private-t21" not in outbound

    detail = requester.get(f"/api/requests/{request_id}")
    assert detail.status_code == 200
    assert detail.json()["decision_packet"]["packet_id"] == packet["packet_id"]
    assert len(model.calls) == 1


def test_no_key_and_unknown_citation_are_honestly_unavailable(
    database_session_factory: sessionmaker[Session],
) -> None:
    no_key = build_client(database_session_factory)
    no_key_request = submit_request(no_key, database_session_factory)
    unavailable = no_key.post(
        f"/api/requests/{no_key_request}/decision-packet"
    )
    assert unavailable.status_code == 201
    assert unavailable.json()["generation_mode"] == "unavailable"
    assert unavailable.json()["advisory"] is None
    assert "不可用" in unavailable.json()["availability_message"]
    assert {
        item["generation_mode"]
        for item in unavailable.json()["items"]
        if item["source_kind"] == "advisory"
    } == {"unavailable"}

    bad_model = RecordingAdvisoryModel(
        DecisionAdvisory(
            assessment="clear",
            summary="模型声称可放行，但不得改变路线。",
            unknowns=[],
            recommendations=[],
            citations=["POL-999"],
        )
    )
    second = build_client(
        database_session_factory,
        advisory_model=bad_model,
    )
    second_request = submit_request(second, database_session_factory)
    unknown_citation = second.post(
        f"/api/requests/{second_request}/decision-packet"
    )
    assert unknown_citation.status_code == 201
    assert unknown_citation.json()["generation_mode"] == "unavailable"
    assert unknown_citation.json()["advisory"] is None
    unknown_route = [
        (step["approver_id"], step["approver_role"])
        for step in unknown_citation.json()["fixed_route"]
    ]
    assert unknown_route == [
        ("EMP-002", "manager"),
        ("EMP-003", "data_owner"),
    ]


def test_timeout_or_invalid_provider_response_persists_unavailable_packet(
    database_session_factory: sessionmaker[Session],
) -> None:
    for provider_error in (TimeoutError("timeout"), ValueError("invalid schema")):
        model = FailingAdvisoryModel(provider_error)
        requester = build_client(database_session_factory, advisory_model=model)
        request_id = submit_request(requester, database_session_factory)

        response = requester.post(f"/api/requests/{request_id}/decision-packet")

        assert response.status_code == 201
        assert response.json()["generation_mode"] == "unavailable"
        assert response.json()["advisory"] is None
        assert model.calls == 1


def test_packet_acl_and_state_checks_happen_before_model_or_write(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    requester = build_client(database_session_factory, advisory_model=model)
    request_id = submit_request(requester, database_session_factory)
    manager = TestClient(requester.app)
    login_as(manager, "EMP-002")

    with database_session_factory() as session:
        before = session.scalar(select(func.count()).select_from(DecisionPacketRecord))
    denied = manager.post(f"/api/requests/{request_id}/decision-packet")
    assert denied.status_code == 404
    assert model.calls == []

    with database_session_factory() as session:
        request = session.get(AccessRequestRecord, UUID(request_id))
        assert request is not None
        request.request_status = "cancelled"
        session.commit()
    wrong_state = requester.post(f"/api/requests/{request_id}/decision-packet")
    assert wrong_state.status_code == 409
    assert model.calls == []
    with database_session_factory() as session:
        after = session.scalar(select(func.count()).select_from(DecisionPacketRecord))
    assert after == before


def test_get_never_generates_packet_or_calls_model(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    requester = build_client(database_session_factory, advisory_model=model)
    request_id = submit_request(requester, database_session_factory)

    detail = requester.get(f"/api/requests/{request_id}")

    assert detail.status_code == 200
    assert detail.json()["decision_packet"] is None
    assert model.calls == []
    with database_session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(DecisionPacketRecord)
            .where(DecisionPacketRecord.request_id == UUID(request_id))
        ) == 0


def test_approval_requires_packet_and_never_reconsults_advisory(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    requester = build_client(database_session_factory, advisory_model=model)
    request_id = submit_request(requester, database_session_factory)

    missing = requester.post(f"/api/requests/{request_id}/approval-case")
    assert missing.status_code == 409
    assert model.calls == []
    packet = requester.post(f"/api/requests/{request_id}/decision-packet")
    assert packet.status_code == 201
    assert packet.json()["advisory"]["assessment"] == "blocked"

    started = requester.post(f"/api/requests/{request_id}/approval-case")

    assert started.status_code == 201, started.text
    assert len(model.calls) == 1
    assert [(step["approver_id"], step["approver_role"]) for step in started.json()["steps"]] == [
        ("EMP-002", "manager"),
        ("EMP-003", "data_owner"),
    ]


def test_requester_new_session_can_generate_packet_and_start_source_case(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    original = build_client(database_session_factory, advisory_model=model)
    request_id = submit_request(original, database_session_factory)
    with database_session_factory() as session:
        request = session.get(AccessRequestRecord, UUID(request_id))
        assert request is not None
        source_workspace_id = request.workspace_id

    requester_relogin = TestClient(original.app)
    login_as(requester_relogin, "EMP-001")
    packet = requester_relogin.post(
        f"/api/requests/{request_id}/decision-packet"
    )
    started = requester_relogin.post(f"/api/requests/{request_id}/approval-case")

    assert packet.status_code == 201
    assert started.status_code == 201
    with database_session_factory() as session:
        case = session.scalar(
            select(ApprovalCaseRecord).where(
                ApprovalCaseRecord.request_id == UUID(request_id)
            )
        )
        assert case is not None
        assert case.workspace_id == source_workspace_id


def test_concurrent_generation_persists_one_packet_and_calls_adapter_once(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    requester = build_client(database_session_factory, advisory_model=model)
    request_id = submit_request(requester, database_session_factory)
    embedding_model = DeterministicEmbeddingModel()

    def generate() -> tuple[str, bool]:
        with database_session_factory() as session:
            packet, created = generate_decision_packet(
                session,
                request_id=UUID(request_id),
                actor_id="EMP-001",
                embedding_model=embedding_model,
                advisory_model=model,
            )
            return str(packet.id), created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: generate(), range(2)))

    assert len({packet_id for packet_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]
    assert len(model.calls) == 1
    with database_session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(DecisionPacketRecord)
            .where(DecisionPacketRecord.request_id == UUID(request_id))
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(ApprovalCaseRecord)
            .where(ApprovalCaseRecord.request_id == UUID(request_id))
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(
                AuditEventRecord.request_id == UUID(request_id),
                AuditEventRecord.event_type == "decision_packet.created",
            )
        ) == 1


def test_identity_or_route_injection_is_rejected_before_advisory(
    database_session_factory: sessionmaker[Session],
) -> None:
    model = RecordingAdvisoryModel()
    requester = build_client(database_session_factory, advisory_model=model)
    request_id = submit_request(requester, database_session_factory)

    injected_query = requester.post(
        f"/api/requests/{request_id}/decision-packet?role=permissions_admin"
    )
    injected_body = requester.post(
        f"/api/requests/{request_id}/decision-packet",
        json={"generation_mode": "provider", "fixed_route": []},
    )

    assert injected_query.status_code == 422
    assert injected_body.status_code == 422
    assert model.calls == []
    with database_session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(DecisionPacketRecord)
            .where(DecisionPacketRecord.request_id == UUID(request_id))
        ) == 0
