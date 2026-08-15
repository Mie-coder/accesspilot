from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    AuditEventRecord,
    ProvisioningAttemptRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog
from accesspilot.provisioning import (
    IamOutcome,
    ProvisioningAttemptNotFoundError,
    ProvisioningNotFoundError,
    provision_access,
    recover_provisioning,
)


class SequencedIamProvisioner:
    def __init__(
        self,
        *,
        provision_outcomes: list[IamOutcome],
        query_outcomes: list[IamOutcome] | None = None,
    ) -> None:
        self.provision_outcomes = provision_outcomes
        self.query_outcomes = query_outcomes or []
        self.provision_calls: list[str] = []
        self.query_calls: list[str] = []

    def provision(
        self,
        *,
        request_id: UUID,
        idempotency_key: str,
        fault_mode: str | None,
    ) -> IamOutcome:
        self.provision_calls.append(idempotency_key)
        return self.provision_outcomes.pop(0)

    def query_status(self, *, idempotency_key: str) -> IamOutcome:
        self.query_calls.append(idempotency_key)
        return self.query_outcomes.pop(0)


def create_request(
    session: Session,
    *,
    approval_status: str = "approved",
) -> tuple[str, UUID]:
    seed_catalog(session)
    token = f"provisioning-{uuid4()}"
    workspace = WorkspaceRecord(token_hash=sha256(token.encode()).hexdigest())
    session.add(workspace)
    session.flush()
    request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id="EMP-001",
        entitlement_code="insighthub.customer_export",
        duration_days=14,
        justification="核验虚构项目运营数据",
        request_status="submitted",
        confirmed_at=datetime.now(UTC),
    )
    session.add(request)
    session.flush()
    session.add(
        ApprovalCaseRecord(
            workspace_id=workspace.id,
            request_id=request.id,
            approval_status=approval_status,
        )
    )
    session.commit()
    return token, request.id


def count_for_request(session: Session, model: type[object], request_id: UUID) -> int:
    return session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.request_id == request_id)  # type: ignore[attr-defined]
    ) or 0


def event_types(session: Session, request_id: UUID) -> list[str]:
    return list(
        session.scalars(
            select(AuditEventRecord.event_type)
            .where(AuditEventRecord.request_id == request_id)
            .order_by(AuditEventRecord.created_at)
        ).all()
    )


def test_unapproved_request_cannot_start_provisioning(
    database_session: Session,
) -> None:
    _token, request_id = create_request(
        database_session,
        approval_status="pending_manager",
    )
    client = SequencedIamProvisioner(
        provision_outcomes=[IamOutcome(status="succeeded")]
    )

    with pytest.raises(ProvisioningNotFoundError):
        provision_access(
            database_session,
            request_id=request_id,
            actor_id="EMP-004",
            roles=("permissions_admin",),
            iam=client,
        )

    assert client.provision_calls == []
    assert count_for_request(database_session, AccessGrantRecord, request_id) == 0
    assert count_for_request(database_session, ProvisioningAttemptRecord, request_id) == 0


def test_success_is_idempotent_and_creates_exactly_one_grant(
    database_session: Session,
) -> None:
    _token, request_id = create_request(database_session)
    idempotency_key = f"accesspilot:{request_id}"
    client = SequencedIamProvisioner(
        provision_outcomes=[IamOutcome(status="succeeded")]
    )

    first = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )
    second = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )

    assert first.id == second.id
    assert second.provisioning_status == "succeeded"
    assert client.provision_calls == [idempotency_key]
    assert count_for_request(database_session, AccessGrantRecord, request_id) == 1
    assert count_for_request(database_session, ProvisioningAttemptRecord, request_id) == 1
    assert "provisioning.started" in event_types(database_session, request_id)
    assert "provisioning.succeeded" in event_types(database_session, request_id)


def test_timeout_stays_unknown_until_query_recovers_same_operation(
    database_session: Session,
) -> None:
    _token, request_id = create_request(database_session)
    idempotency_key = f"accesspilot:{request_id}"
    client = SequencedIamProvisioner(
        provision_outcomes=[IamOutcome(status="unknown", message="响应超时")],
        query_outcomes=[IamOutcome(status="succeeded")],
    )

    unknown = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )
    replay = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )

    assert unknown.provisioning_status == "unknown"
    assert replay.provisioning_status == "unknown"
    assert client.provision_calls == [idempotency_key]
    assert count_for_request(database_session, AccessGrantRecord, request_id) == 0

    recovered = recover_provisioning(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )
    recovered_again = recover_provisioning(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )

    assert recovered.provisioning_status == "succeeded"
    assert recovered_again.provisioning_status == "succeeded"
    assert client.query_calls == [idempotency_key]
    assert count_for_request(database_session, AccessGrantRecord, request_id) == 1
    assert "provisioning.unknown" in event_types(database_session, request_id)
    assert "provisioning.succeeded" in event_types(database_session, request_id)


def test_failed_attempt_retries_with_same_key_and_can_recover(
    database_session: Session,
) -> None:
    _token, request_id = create_request(database_session)
    idempotency_key = f"accesspilot:{request_id}"
    client = SequencedIamProvisioner(
        provision_outcomes=[
            IamOutcome(status="failed", message="IAM 暂时失败"),
            IamOutcome(status="succeeded"),
        ]
    )

    failed = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )
    assert failed.provisioning_status == "failed"
    recovered = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )

    assert recovered.provisioning_status == "succeeded"
    assert recovered.attempt_count == 2
    assert client.provision_calls == [idempotency_key, idempotency_key]
    assert count_for_request(database_session, AccessGrantRecord, request_id) == 1
    assert "provisioning.failed" in event_types(database_session, request_id)


def test_recover_requires_an_existing_attempt(database_session: Session) -> None:
    _token, request_id = create_request(database_session)
    client = SequencedIamProvisioner(provision_outcomes=[])

    with pytest.raises(ProvisioningAttemptNotFoundError):
        recover_provisioning(
            database_session,
            request_id=request_id,
            actor_id="EMP-004",
            roles=("permissions_admin",),
            iam=client,
        )


def test_existing_legacy_grant_is_reconciled_without_calling_iam(
    database_session: Session,
) -> None:
    _token, request_id = create_request(database_session)
    key = f"accesspilot:{request_id}"
    request = database_session.get(AccessRequestRecord, request_id)
    assert request is not None
    now = datetime.now(UTC)
    database_session.add(
        AccessGrantRecord(
            workspace_id=request.workspace_id,
            request_id=request.id,
            idempotency_key=key,
            starts_at=now,
            expires_at=now + timedelta(days=request.duration_days),
        )
    )
    database_session.commit()
    client = SequencedIamProvisioner(
        provision_outcomes=[IamOutcome(status="succeeded")]
    )

    attempt = provision_access(
        database_session,
        request_id=request_id,
        actor_id="EMP-004",
        roles=("permissions_admin",),
        iam=client,
    )

    assert attempt.provisioning_status == "succeeded"
    assert client.provision_calls == []
    assert count_for_request(database_session, AccessGrantRecord, request_id) == 1
    assert count_for_request(database_session, ProvisioningAttemptRecord, request_id) == 1
    assert "provisioning.reconciled" in event_types(database_session, request_id)
