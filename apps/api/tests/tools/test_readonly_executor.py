"""只读工具执行器的白名单与后端身份注入合同。"""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from accesspilot.db.models import AccessRequestRecord, WorkspaceRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.tools.executor import (
    ReadOnlyToolCall,
    UnknownReadOnlyToolError,
    execute_read_only_tool,
    tool_call_for_intent,
)


def create_workspace(database_session: Session, *, actor_id: str = "EMP-001") -> str:
    token = f"readonly-executor-{uuid4()}"
    database_session.add(
        WorkspaceRecord(
            token_hash=sha256(token.encode()).hexdigest(),
            actor_id=actor_id,
        )
    )
    database_session.commit()
    return token


def test_executor_injects_workspace_identity_without_model_arguments(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    token = create_workspace(database_session)

    result = execute_read_only_tool(
        database_session,
        workspace_token=token,
        call=ReadOnlyToolCall(tool="list_eligible_access"),
    )

    assert result.status == "success"
    assert result.eligible_access is not None
    assert [item.code for item in result.eligible_access] == [
        "codeforge.repo_read",
        "insighthub.customer_export",
        "insighthub.dashboard_view",
    ]


def test_executor_rejects_unknown_tools_and_identity_arguments() -> None:
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate({"tool": "approve_access"})
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate({"tool": "list_active_access", "employee_id": "EMP-003"})


def test_latest_status_is_scoped_to_the_workspace_backend_identity(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    token = create_workspace(database_session)
    workspace = (
        database_session.query(WorkspaceRecord)
        .filter_by(token_hash=sha256(token.encode()).hexdigest())
        .one()
    )
    own_request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id="EMP-001",
        entitlement_code="insighthub.dashboard_view",
        duration_days=14,
        justification="虚构申请状态查询",
        request_status="submitted",
        confirmed_at=datetime.now(UTC),
    )
    other_employee_request = AccessRequestRecord(
        workspace_id=workspace.id,
        requester_id="EMP-002",
        entitlement_code="codeforge.repo_read",
        duration_days=14,
        justification="不应被当前身份读取",
        request_status="approved",
        confirmed_at=datetime.now(UTC),
    )
    database_session.add_all([own_request, other_employee_request])
    database_session.commit()

    result = execute_read_only_tool(
        database_session,
        workspace_token=token,
        call=ReadOnlyToolCall(tool="get_latest_request_status"),
    )

    assert result.status == "success"
    assert result.request_status is not None
    assert result.request_status.request_id == str(own_request.id)
    assert result.request_status.request_status == "submitted"


def test_write_intents_never_map_to_a_read_only_tool() -> None:
    assert tool_call_for_intent("request_access") is None
    assert tool_call_for_intent("security_probe") is None


def test_executor_fails_closed_if_schema_validation_is_bypassed(
    database_session: Session,
) -> None:
    call = ReadOnlyToolCall.model_construct(tool="approve_access")

    with pytest.raises(UnknownReadOnlyToolError):
        execute_read_only_tool(
            database_session,
            workspace_token="unused-token",
            call=call,
        )


def test_entitlement_resolver_tool_requires_strict_query_without_identity_argument() -> None:
    call = ReadOnlyToolCall.model_validate(
        {
            "tool": "resolve_entitlement",
            "query": "仪表盘查看",
        }
    )

    assert call.tool == "resolve_entitlement"
    assert call.query == "仪表盘查看"

    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "resolve_entitlement", "query": 123}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {
                "tool": "resolve_entitlement",
                "query": "仪表盘查看",
                "employee_id": "EMP-003",
            }
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "resolve_entitlement", "query": ""}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "resolve_entitlement"}
        )
