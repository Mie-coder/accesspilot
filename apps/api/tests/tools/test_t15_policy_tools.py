"""T15 政策只读工具严格 Schema 红测。

这些测试故意只描述服务端可接受的工具合同：策略目录与检索是只读能力，
调用方不能注入员工身份、写操作或未声明参数。
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.orm import Session

from accesspilot.agent.embeddings import DeterministicEmbeddingModel
from accesspilot.db.models import PolicyChunkRecord
from accesspilot.db.seed import seed_catalog
from accesspilot.rag.policies import index_policy_embeddings
from accesspilot.tools.executor import (
    ReadOnlyToolCall,
    execute_read_only_tool,
    tool_call_for_intent,
)


def test_policy_tools_are_whitelisted_without_client_identity(
    database_session: Session,
) -> None:
    seed_catalog(database_session)

    catalog_call = ReadOnlyToolCall(tool="list_policy_catalog")
    self_approval_call = ReadOnlyToolCall(tool="get_self_approval_policy")
    query_call = ReadOnlyToolCall(tool="search_policies", query="审批要求")

    assert catalog_call.query is None
    assert self_approval_call.query is None
    assert query_call.query == "审批要求"
    policy_tool = tool_call_for_intent("policy_question")
    assert policy_tool is not None
    assert policy_tool.tool in {
        "list_policy_catalog",
        "get_self_approval_policy",
        "search_policies",
    }


def test_policy_tool_schema_forbids_query_for_catalog_and_unknown_arguments() -> None:
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "list_policy_catalog", "query": "POL-001"}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "search_policies", "query": "审批", "employee_id": "EMP-003"}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate({"tool": "search_policies"})
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "get_self_approval_policy", "query": "POL-006"}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "list_policy_catalog", "employee_id": "EMP-003"}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "search_policies", "query": 123}
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolCall.model_validate(
            {"tool": "search_policies", "query": "审批", "write": True}
        )


def test_policy_tools_execute_from_server_injected_service(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    assert index_policy_embeddings(
        database_session,
        DeterministicEmbeddingModel(),
    ) == 8

    catalog = execute_read_only_tool(
        database_session,
        workspace_token="unused-for-policy-facts",
        call=ReadOnlyToolCall(tool="list_policy_catalog"),
    )
    self_approval = execute_read_only_tool(
        database_session,
        workspace_token="unused-for-policy-facts",
        call=ReadOnlyToolCall(tool="get_self_approval_policy"),
    )
    searched = execute_read_only_tool(
        database_session,
        workspace_token="unused-for-policy-facts",
        call=ReadOnlyToolCall(tool="search_policies", query="客户数据导出审批"),
    )

    assert catalog.status == "success"
    assert catalog.policy_catalog is not None
    assert len(catalog.policy_catalog) == 8
    assert self_approval.status == "success"
    assert self_approval.policy_answer is not None
    assert [item.policy_code for item in self_approval.policy_answer.evidence] == [
        "POL-006"
    ]
    assert searched.status == "success"
    assert searched.policy_answer is not None
    assert searched.policy_answer.status == "grounded"
    assert searched.policy_answer.evidence
    assert all(item.policy_code.startswith("POL-") for item in searched.policy_answer.evidence)


def test_policy_catalog_tool_fails_closed_when_fact_source_is_partial(
    database_session: Session,
) -> None:
    seed_catalog(database_session)
    database_session.execute(
        delete(PolicyChunkRecord).where(
            PolicyChunkRecord.policy_code == "POL-008"
        )
    )
    database_session.flush()

    result = execute_read_only_tool(
        database_session,
        workspace_token="unused-for-policy-facts",
        call=ReadOnlyToolCall(tool="list_policy_catalog"),
    )

    assert result.status == "success"
    assert result.policy_catalog is None
    assert result.policy_answer is not None
    assert result.policy_answer.status == "retrieval_unavailable"
    assert result.policy_answer.evidence == []
