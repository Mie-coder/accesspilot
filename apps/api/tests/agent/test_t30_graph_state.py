from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ValidationError

from accesspilot.agent.checkpoint import (
    FencedPostgresSaverAdapter,
    ServerExecutionContext,
)
from accesspilot.db.models import AgentTurnExecutionRecord

EXPECTED_STATE_FIELDS = {
    "schema_version",
    "flow_version",
    "workspace_ref",
    "graph_run_id",
    "input_seq",
    "input_turn_id",
    "safe_user_text",
    "input_kind",
    "intent",
    "security_flagged",
    "selected_route",
    "base_draft_revision",
    "committed_draft_revision",
    "draft_patch",
    "missing_fields",
    "phase",
    "tool_name",
    "safe_tool_result",
    "policy_status",
    "policy_evidence_codes",
    "policy_match_count",
    "business_status",
    "assistant_message",
    "recoverable_error",
    "pending_input_id",
    "pending_input_kind",
}

EXPECTED_NODE_NAMES = {
    "hydrate_authoritative_snapshot",
    "route_intent",
    "compose_safe_answer",
    "handle_numeric_followup",
    "select_read_tool",
    "execute_read_tool",
    "retrieve_policy_pgvector",
    "grade_policy_evidence",
    "compose_grounded_answer",
    "compose_insufficient_answer",
    "compose_recoverable_answer",
    "parse_request_patch",
    "resolve_entitlement",
    "merge_candidate",
    "persist_draft_cas",
    "validate_draft",
    "ask_missing_field",
    "await_requester_confirmation",
    "rehydrate_resume_snapshot",
    "apply_confirmation_cas",
    "ready_to_submit",
    "finalize_public_outcome",
}

EXPECTED_CONDITIONAL_PATHS = {
    "route_intent": {
        "security": "compose_safe_answer",
        "help": "compose_safe_answer",
        "unknown": "compose_safe_answer",
        "numeric_cursor": "handle_numeric_followup",
        "read_only": "select_read_tool",
        "policy": "retrieve_policy_pgvector",
        "request_access": "parse_request_patch",
    },
    "grade_policy_evidence": {
        "grounded": "compose_grounded_answer",
        "insufficient": "compose_insufficient_answer",
        "unavailable": "compose_recoverable_answer",
    },
    "request_needs_entitlement_resolution": {
        "yes": "resolve_entitlement",
        "no": "merge_candidate",
    },
    "validate_draft": {
        "missing": "ask_missing_field",
        "invalid_or_conflict": "compose_recoverable_answer",
        "complete": "await_requester_confirmation",
    },
    "rehydrate_resume_snapshot": {
        "confirm": "apply_confirmation_cas",
        "non_confirm_input": "route_intent",
    },
    "apply_confirmation_cas": {
        "success": "ready_to_submit",
        "conflict": "compose_recoverable_answer",
    },
}

EXPECTED_UNCONDITIONAL_EDGES = {
    ("__start__", "hydrate_authoritative_snapshot"),
    ("hydrate_authoritative_snapshot", "route_intent"),
    ("select_read_tool", "execute_read_tool"),
    ("execute_read_tool", "compose_safe_answer"),
    ("retrieve_policy_pgvector", "grade_policy_evidence"),
    ("resolve_entitlement", "merge_candidate"),
    ("merge_candidate", "persist_draft_cas"),
    ("persist_draft_cas", "validate_draft"),
    # T34: the confirmation interrupt node has no static outgoing edge; resume
    # re-enters it and routes via Command(goto=rehydrate_resume_snapshot).
    # LangGraph renders the node's implicit terminal edge to END.
    ("await_requester_confirmation", "__end__"),
    ("compose_safe_answer", "finalize_public_outcome"),
    ("handle_numeric_followup", "finalize_public_outcome"),
    ("compose_grounded_answer", "finalize_public_outcome"),
    ("compose_insufficient_answer", "finalize_public_outcome"),
    ("compose_recoverable_answer", "finalize_public_outcome"),
    ("ask_missing_field", "finalize_public_outcome"),
    ("finalize_public_outcome", "__end__"),
}


def _input(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "flow_version": 2,
        "workspace_ref": uuid4(),
        "graph_run_id": uuid4(),
        "input_seq": 0,
        "input_turn_id": str(uuid4()),
        "safe_user_text": "请帮我处理虚构权限业务",
        "input_kind": "new_input",
    }
    value.update(updates)
    return value


def _full_state(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        **_input(),
        "intent": "request_access",
        "security_flagged": False,
        "selected_route": "request_access",
        "base_draft_revision": 0,
        "committed_draft_revision": None,
        "draft_patch": {
            "entitlement_id": "insighthub.customer_export",
            "duration_days": 30,
            "justification": "核验虚构项目数据",
        },
        "missing_fields": [],
        "phase": "awaiting_confirmation",
        "tool_name": None,
        "safe_tool_result": None,
        "policy_status": None,
        "policy_evidence_codes": [],
        "policy_match_count": 0,
        "business_status": "awaiting_confirmation",
        "assistant_message": "请确认虚构申请。",
        "recoverable_error": None,
        "pending_input_id": None,
        "pending_input_kind": None,
    }
    value.update(updates)
    return value


def _runtime_context() -> dict[str, object]:
    marker = object()
    return {
        "session_factory": marker,
        "workspace_service": marker,
        "policy_service": marker,
        "structured_reply_model": marker,
        "intent_router": marker,
        "principal": marker,
        "current_turn_id": "runtime-turn-secret",
        "current_fence": 91,
        "workspace_token": "workspace-token-canary",
        "auth_session_id": "auth-session-canary",
        "cookie": "cookie-canary",
        "csrf_token": "csrf-canary",
        "api_key": "sk-runtime-canary-12345678",
    }


def test_graph_schemas_are_strict_frozen_and_state_has_exact_spec_fields() -> None:
    from accesspilot.agent.production_graph import GraphInput, GraphOutput
    from accesspilot.agent.state import DraftPatch, GraphState, SafeToolResult

    assert set(GraphState.model_fields) == EXPECTED_STATE_FIELDS
    for schema in (GraphInput, GraphState, GraphOutput, DraftPatch, SafeToolResult):
        assert schema.model_config["extra"] == "forbid"
        assert schema.model_config["strict"] is True
        assert schema.model_config["frozen"] is True


@pytest.mark.parametrize(
    "forbidden_key",
    [
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
        "principal",
        "workspace_token",
        "csrf_token",
        "api_key",
    ],
)
def test_graph_state_rejects_every_forbidden_top_level_field(
    forbidden_key: str,
) -> None:
    from accesspilot.agent.state import GraphState

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GraphState.model_validate(_full_state(**{forbidden_key: "canary"}))


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("input_seq", True),
        ("flow_version", "2"),
        ("security_flagged", 1),
        ("policy_match_count", -1),
        ("base_draft_revision", -1),
        ("workspace_ref", "workspace-token-canary"),
        ("graph_run_id", "admin"),
        ("pending_input_id", "cookie-canary"),
        ("missing_fields", ["employee_id"]),
        ("missing_fields", ["justification", "justification"]),
        ("policy_evidence_codes", [f"POL-{index:03d}" for index in range(9)]),
    ],
)
def test_graph_state_rejects_wrong_types_bounds_and_unapproved_list_values(
    field: str,
    bad_value: object,
) -> None:
    from accesspilot.agent.state import GraphState

    with pytest.raises(ValidationError):
        GraphState.model_validate(_full_state(**{field: bad_value}))


@pytest.mark.parametrize(
    "extra",
    [
        {"employee_id": "EMP-001"},
        {"role": "admin"},
        {"confirmed": True},
        {"provider_result": {"hidden": "value"}},
    ],
)
def test_draft_patch_rejects_identity_confirmation_and_provider_fields(
    extra: dict[str, object],
) -> None:
    from accesspilot.agent.state import DraftPatch

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DraftPatch.model_validate(
            {
                "entitlement_id": "insighthub.customer_export",
                "duration_days": 30,
                "justification": "核验虚构项目数据",
                **extra,
            }
        )


def test_draft_patch_allows_bounded_natural_language_until_resolution_node() -> None:
    from accesspilot.agent.production_graph import _route_entitlement_resolution
    from accesspilot.agent.state import DraftPatch, GraphState

    patch = DraftPatch(
        entitlement_id="客户数据导出",
        duration_days=30,
        justification="核验虚构项目数据",
    )
    state = GraphState.model_validate(_full_state(draft_patch=patch))

    assert patch.entitlement_id == "客户数据导出"
    assert _route_entitlement_resolution(state) == "yes"


@pytest.mark.parametrize(
    "extra",
    [
        {"query": "原始查询"},
        {"policy_body": "完整政策正文"},
        {"row": {"employee_id": "EMP-001"}},
        {"vector": [0.1, 0.2]},
        {"provider_result": {"raw": True}},
    ],
)
def test_safe_tool_result_rejects_raw_tool_and_provider_material(
    extra: dict[str, object],
) -> None:
    from accesspilot.agent.state import SafeToolResult

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SafeToolResult.model_validate(
            {
                "tool": "list_eligible_access",
                "status": "success",
                "entitlement_codes": ["insighthub.customer_export"],
                "policy_codes": [],
                "match_count": 1,
                "summary": "命中 1 个虚构权限。",
                **extra,
            }
        )


def test_safe_text_fields_use_existing_credential_redaction_before_state() -> None:
    from accesspilot.agent.production_graph import GraphInput
    from accesspilot.agent.state import DraftPatch, SafeToolResult

    secret = "sk-demo-secret-123456"
    graph_input = GraphInput.model_validate(_input(safe_user_text=f"请查看 API_KEY={secret}"))
    patch = DraftPatch(
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification=f"核验数据，临时凭证是 {secret}",
    )
    result = SafeToolResult(
        tool="list_eligible_access",
        status="success",
        entitlement_codes=[],
        policy_codes=[],
        match_count=0,
        summary=f"查询完成 {secret}",
    )

    assert secret not in graph_input.safe_user_text
    assert secret not in patch.justification
    assert secret not in result.summary


def test_runtime_only_names_are_absent_from_all_persistable_schemas() -> None:
    from accesspilot.agent.production_graph import GraphInput, GraphOutput
    from accesspilot.agent.state import GraphState

    runtime_only = set(_runtime_context())
    for schema in (GraphInput, GraphState, GraphOutput):
        assert runtime_only.isdisjoint(schema.model_fields)


class _NoWriteSaver(BaseCheckpointSaver[str]):
    def __init__(self) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self.put_calls = 0
        self.write_calls = 0

    def get_tuple(self, config: dict[str, Any]) -> None:
        del config
        return None

    def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        del config, checkpoint, metadata, new_versions
        self.put_calls += 1
        raise AssertionError("invalid input reached checkpoint put")

    def put_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        self.write_calls += 1
        raise AssertionError("invalid input reached checkpoint put_writes")

    def get_next_version(self, current: str | None, channel: None) -> str:
        del channel
        return "1" if current is None else str(int(current) + 1)


@dataclass(frozen=True)
class _UnknownDataclass:
    value: str


class _UnknownPydantic(BaseModel):
    value: str


@pytest.mark.parametrize(
    "malicious_update",
    [
        {"employee_id": "EMP-001"},
        {"role": "permissions_admin"},
        {"confirmed": True},
        {"safe_user_text": object()},
        {"safe_user_text": {"not", "a", "string"}},
        {"safe_user_text": b"pickle-like-bytes"},
        {"safe_user_text": _UnknownDataclass("canary")},
        {"safe_user_text": _UnknownPydantic(value="canary")},
        {"workspace_ref": "workspace-token-canary"},
        {"graph_run_id": "admin"},
        {"input_turn_id": "cookie-canary"},
    ],
)
def test_production_invoke_preflights_before_any_saver_write(
    malicious_update: dict[str, object],
) -> None:
    from accesspilot.agent.production_graph import build_production_graph

    saver = _NoWriteSaver()
    graph = build_production_graph(checkpointer=saver)

    with pytest.raises(ValidationError):
        graph.invoke(
            _input(**malicious_update),
            config={"configurable": {"thread_id": "accesspilot:v1.3:invalid"}},
            context=_runtime_context(),
        )

    assert saver.put_calls == 0
    assert saver.write_calls == 0


def test_production_graph_has_exact_nodes_condition_maps_and_representative_paths() -> None:
    from accesspilot.agent.production_graph import (
        PRODUCTION_CONDITIONAL_PATHS,
        PRODUCTION_NODE_NAMES,
        REPRESENTATIVE_PATHS,
        build_production_graph,
    )

    graph = build_production_graph(checkpointer=False)

    assert isinstance(graph.compiled, CompiledStateGraph)
    assert set(PRODUCTION_NODE_NAMES) == EXPECTED_NODE_NAMES
    assert PRODUCTION_CONDITIONAL_PATHS == EXPECTED_CONDITIONAL_PATHS
    rendered = graph.compiled.get_graph().to_json()
    actual_nodes = {
        node["id"] for node in rendered["nodes"] if node["id"] not in {"__start__", "__end__"}
    }
    assert actual_nodes == EXPECTED_NODE_NAMES
    actual_unconditional_edges = {
        (edge["source"], edge["target"])
        for edge in rendered["edges"]
        if not edge.get("conditional", False)
    }
    assert actual_unconditional_edges == EXPECTED_UNCONDITIONAL_EDGES
    actual_condition_maps = {
        source: next(iter(branches.values())).ends
        for source, branches in graph.compiled.builder.branches.items()
    }
    assert actual_condition_maps == {
        "route_intent": EXPECTED_CONDITIONAL_PATHS["route_intent"],
        "grade_policy_evidence": EXPECTED_CONDITIONAL_PATHS["grade_policy_evidence"],
        "parse_request_patch": EXPECTED_CONDITIONAL_PATHS["request_needs_entitlement_resolution"],
        "validate_draft": EXPECTED_CONDITIONAL_PATHS["validate_draft"],
        "rehydrate_resume_snapshot": EXPECTED_CONDITIONAL_PATHS["rehydrate_resume_snapshot"],
        "apply_confirmation_cas": EXPECTED_CONDITIONAL_PATHS["apply_confirmation_cas"],
    }
    actual_edges = {(edge["source"], edge["target"]) for edge in rendered["edges"]}
    for path in REPRESENTATIVE_PATHS.values():
        assert all(edge in actual_edges for edge in pairwise(path))


def test_reusable_node_gate_rejects_invalid_updates_before_returning_to_langgraph() -> None:
    from accesspilot.agent.production_graph import (
        UnsafeGraphStateUpdateError,
        validated_state_node,
    )
    from accesspilot.agent.state import GraphState

    state = GraphState.model_validate(_full_state())

    def malicious_stub(state, runtime):  # type: ignore[no-untyped-def]
        del state, runtime
        return {"draft_patch": {"employee_id": "EMP-001"}}

    gated = validated_state_node(malicious_stub)
    with pytest.raises(UnsafeGraphStateUpdateError, match="invalid state update"):
        gated(state, None)  # type: ignore[arg-type]


class _ExplodingDependency:
    calls = 0

    def __getattribute__(self, name: str) -> object:
        if name == "calls":
            return object.__getattribute__(self, name)
        type(self).calls += 1
        raise AssertionError(f"stub graph touched runtime dependency: {name}")


def test_safe_graph_invokes_without_models_tools_rag_writes_or_interrupts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from accesspilot.agent.embeddings import DeterministicEmbeddingModel
    from accesspilot.agent.production_graph import GraphOutput, build_production_graph
    from accesspilot.agent.routing import DeterministicIntentRouter
    from accesspilot.auth import Principal
    from accesspilot.events import ModelQuota
    from accesspilot.tools.policies import PolicyService
    from accesspilot.workspaces import InMemoryWorkspaceStore, WorkspaceService

    _ExplodingDependency.calls = 0
    dependency = _ExplodingDependency()
    workspace_service = WorkspaceService(InMemoryWorkspaceStore())
    workspace = workspace_service.create()
    monkeypatch.setattr(
        "accesspilot.agent.production_graph.get_model_quota",
        lambda *args, **kwargs: ModelQuota(
            used=0,
            limit=20,
            remaining=20,
            retry_consumed=0,
        ),
    )
    context = {
        "session_factory": lambda: nullcontext(object()),
        "workspace_service": workspace_service,
        "policy_service": PolicyService(
            embedding_model=DeterministicEmbeddingModel()
        ),
        "structured_reply_model": dependency,
        "intent_router": DeterministicIntentRouter(),
        "principal": Principal(
            employee_id="EMP-001",
            name="林晓",
            department="数据平台部",
            roles=("analyst",),
        ),
        "current_turn_id": "turn-runtime-only",
        "current_fence": 12,
        "workspace_token": workspace.token,
        "auth_session_id": "auth-session-runtime-only",
        "cookie": "cookie-runtime-only",
        "csrf_token": "csrf-runtime-only",
        "api_key": "sk-runtime-only-12345678",
    }
    graph = build_production_graph(checkpointer=False)

    result = graph.invoke(
        _input(workspace_ref=workspace.workspace_id, safe_user_text="帮助"),
        context=context,  # type: ignore[arg-type]
    )

    assert isinstance(result, GraphOutput)
    assert result.intent == "help"
    assert result.business_status == "answered"
    assert _ExplodingDependency.calls == 0
    assert "__interrupt__" not in result.model_dump()


class _AllowlistSaver(BaseCheckpointSaver[str]):
    def __init__(self) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self.allowlist_calls: list[set[tuple[str, ...]]] = []
        self.version_calls: list[tuple[str | None, object]] = []

    def with_allowlist(self, extra_allowlist: set[tuple[str, ...]]) -> _AllowlistSaver:
        self.allowlist_calls.append(set(extra_allowlist))
        return copy(super().with_allowlist(extra_allowlist))

    def get_tuple(self, config: dict[str, Any]) -> None:
        del config
        return None

    def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        del config, checkpoint, metadata, new_versions
        raise AssertionError("not used")

    def put_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        raise AssertionError("not used")

    def get_next_version(self, current: str | None, channel: None) -> str:
        self.version_calls.append((current, channel))
        return "00000000000000000000000000000001.0.1"


def _execution_context() -> ServerExecutionContext:
    graph_run_id = uuid4()
    record = AgentTurnExecutionRecord(
        id=uuid4(),
        workspace_id=uuid4(),
        graph_run_id=graph_run_id,
        checkpoint_thread_id=f"accesspilot:v1.3:{graph_run_id}",
        input_seq=0,
        input_turn_id=f"turn-{uuid4()}",
        input_event_id=1,
        auth_session_ref=uuid4(),
        actor_id="EMP-001",
        engine="langgraph",
        attempt=1,
        lease_fence=4,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        status="running",
        checkpoint_ns="",
        accepted_checkpoint_id=None,
        terminal_event_id=None,
    )
    return ServerExecutionContext.from_record(record)


def test_fenced_saver_allowlist_reaches_delegate_and_clone_shares_candidate_holder() -> None:
    from accesspilot.agent.production_graph import build_production_graph

    saver = _AllowlistSaver()
    invocation = FencedPostgresSaverAdapter(saver).for_execution(_execution_context())
    sentinel = object()
    invocation._invocation_state.candidate = sentinel  # type: ignore[assignment]

    graph = build_production_graph(checkpointer=invocation)
    compiled_saver = graph.compiled.checkpointer

    assert compiled_saver is not invocation
    assert compiled_saver._saver is not saver
    assert saver.allowlist_calls
    assert compiled_saver.serde is compiled_saver._saver.serde
    allowed = compiled_saver.serde._allowed_msgpack_modules
    assert allowed is not None and allowed is not True
    assert ("accesspilot.agent.state", "DraftPatch") in allowed
    assert ("accesspilot.agent.state", "SafeToolResult") in allowed
    assert compiled_saver.candidate is sentinel
    assert invocation.candidate is sentinel
    assert compiled_saver.get_next_version(None, None).endswith(".0.1")


def test_strict_serializer_roundtrips_approved_state_types_and_blocks_unknown_or_pickle() -> None:
    from accesspilot.agent.production_graph import build_production_graph
    from accesspilot.agent.state import DraftPatch, SafeToolResult

    saver = _AllowlistSaver()
    invocation = FencedPostgresSaverAdapter(saver).for_execution(_execution_context())
    graph = build_production_graph(checkpointer=invocation)
    serializer = graph.compiled.checkpointer.serde
    patch = DraftPatch(
        entitlement_id="insighthub.customer_export",
        duration_days=30,
        justification="核验虚构项目数据",
    )

    encoded = serializer.dumps_typed(patch)
    assert serializer.loads_typed(encoded) == patch
    tool_result = SafeToolResult(
        tool="list_eligible_access",
        status="success",
        entitlement_codes=["insighthub.customer_export"],
        policy_codes=["POL.ACCESS.001"],
        match_count=1,
        summary="命中 1 个虚构权限。",
    )
    encoded_tool_result = serializer.dumps_typed(tool_result)
    assert serializer.loads_typed(encoded_tool_result) == tool_result

    class UnknownCheckpointClass:
        pass

    with pytest.raises(TypeError, match="not.*serializable"):
        serializer.dumps_typed(UnknownCheckpointClass())
    unknown_model = _UnknownPydantic(value="blocked")
    encoded_unknown = serializer.dumps_typed(unknown_model)
    assert serializer.loads_typed(encoded_unknown) == {"value": "blocked"}
    with pytest.raises(NotImplementedError, match="pickle"):
        serializer.loads_typed(("pickle", b"not-a-pickle"))
