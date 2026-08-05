"""Day 4 SQLAlchemy ORM 表结构测试。"""

from importlib import import_module

from sqlalchemy import CheckConstraint, UniqueConstraint

from accesspilot.db.base import Base


def load_tables():  # type: ignore[no-untyped-def]
    """导入 ORM 模型，让各张表注册到 Base.metadata。"""

    import_module("accesspilot.db.models")
    return Base.metadata.tables


def test_metadata_contains_day4_tables() -> None:
    """ER 图中的 10 张表必须全部进入 ORM 元数据。"""

    assert set(load_tables()) == {
        "access_grants",
        "access_requests",
        "approval_cases",
        "approval_steps",
        "audit_events",
        "employees",
        "entitlements",
        "policy_chunks",
        "systems",
        "workspaces",
    }


def test_workspace_table_stores_isolated_demo_state() -> None:
    """Workspace 应保存 Token 哈希、可变草稿和故障模式。"""

    workspace = load_tables()["workspaces"]

    assert {"id", "token_hash", "draft", "fault_mode", "created_at"} <= set(workspace.c.keys())
    assert workspace.c.token_hash.unique is True
    assert workspace.c.draft.nullable is True
    assert workspace.c.fault_mode.nullable is True


def test_employee_manager_points_back_to_employee_table() -> None:
    """员工的 manager_id 应指向同一张员工表。"""

    employee = load_tables()["employees"]

    assert employee.c.employee_id.primary_key is True
    manager_foreign_keys = list(employee.c.manager_id.foreign_keys)
    assert len(manager_foreign_keys) == 1
    assert manager_foreign_keys[0].target_fullname == "employees.employee_id"
    assert employee.c.manager_id.nullable is True


def test_entitlement_links_to_system_and_optional_owner() -> None:
    """权限必须属于系统，并可以指定一名数据所有者。"""

    tables = load_tables()
    system = tables["systems"]
    entitlement = tables["entitlements"]

    assert system.c.code.primary_key is True
    assert {key.target_fullname for key in entitlement.c.system_code.foreign_keys} == {
        "systems.code"
    }
    assert {key.target_fullname for key in entitlement.c.owner_id.foreign_keys} == {
        "employees.employee_id"
    }
    assert entitlement.c.owner_id.nullable is True
    assert entitlement.c.self_service_allowed.nullable is False


def test_access_request_preserves_confirmed_business_facts() -> None:
    """正式申请应保存确认内容、关联目录并限制正期限。"""

    request = load_tables()["access_requests"]

    assert {
        "id",
        "workspace_id",
        "requester_id",
        "entitlement_code",
        "duration_days",
        "justification",
        "request_status",
        "confirmed_at",
        "created_at",
    } == set(request.c.keys())
    assert {key.target_fullname for key in request.c.workspace_id.foreign_keys} == {"workspaces.id"}
    assert {key.target_fullname for key in request.c.requester_id.foreign_keys} == {
        "employees.employee_id"
    }
    assert {key.target_fullname for key in request.c.entitlement_code.foreign_keys} == {
        "entitlements.code"
    }
    assert any(
        isinstance(constraint, UniqueConstraint)
        and set(constraint.columns.keys()) == {"workspace_id", "id"}
        for constraint in request.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint) and str(constraint.sqltext) == "duration_days > 0"
        for constraint in request.constraints
    )


def test_idempotency_and_approval_order_are_unique() -> None:
    """幂等键和同一审批流的节点顺序不得重复。"""

    tables = load_tables()
    grant = tables["access_grants"]
    step = tables["approval_steps"]

    assert grant.c.idempotency_key.unique is True
    assert any(
        set(constraint.columns.keys()) == {"approval_case_id", "step_order"}
        for constraint in step.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint) and str(constraint.sqltext) == "step_order > 0"
        for constraint in step.constraints
    )


def test_access_grant_only_represents_successful_time_bounded_access() -> None:
    """真实授权应唯一关联申请，并且失效时间晚于生效时间。"""

    grant = load_tables()["access_grants"]

    assert grant.c.request_id.unique is True
    assert grant.c.idempotency_key.unique is True
    assert any(
        isinstance(constraint, CheckConstraint)
        and str(constraint.sqltext) == "expires_at > starts_at"
        for constraint in grant.constraints
    )


def test_audit_event_can_record_workspace_or_request_level_events() -> None:
    """审计事件可以关联申请，也可以只记录 Workspace 事件。"""

    audit = load_tables()["audit_events"]

    assert audit.c.workspace_id.nullable is False
    assert audit.c.request_id.nullable is True
    assert audit.c.actor_type.nullable is False
    assert audit.c.actor_id.nullable is True
    assert audit.c.details.nullable is False


def test_policy_chunk_has_unique_position_and_optional_512_dimension_vector() -> None:
    """政策分块位置不重复，Embedding 固定 512 维且允许待补全。"""

    policy = load_tables()["policy_chunks"]

    assert any(
        isinstance(constraint, UniqueConstraint)
        and set(constraint.columns.keys()) == {"policy_code", "chunk_index"}
        for constraint in policy.constraints
    )
    assert policy.c.embedding.type.dim == 512
    assert policy.c.embedding.nullable is True
    assert policy.c.metadata.nullable is False


def test_workspace_and_request_use_a_composite_foreign_key() -> None:
    """审批流不得引用另一个 Workspace 的申请。"""

    approval_case = load_tables()["approval_cases"]

    assert any(
        {element.parent.name for element in constraint.elements} == {"workspace_id", "request_id"}
        and {element.target_fullname for element in constraint.elements}
        == {"access_requests.workspace_id", "access_requests.id"}
        for constraint in approval_case.foreign_key_constraints
    )

    for table_name in ("access_grants", "audit_events"):
        table = load_tables()[table_name]
        assert any(
            {element.parent.name for element in constraint.elements}
            == {"workspace_id", "request_id"}
            and {element.target_fullname for element in constraint.elements}
            == {"access_requests.workspace_id", "access_requests.id"}
            for constraint in table.foreign_key_constraints
        )

    approval_step = load_tables()["approval_steps"]
    assert any(
        {element.parent.name for element in constraint.elements}
        == {"workspace_id", "approval_case_id"}
        and {element.target_fullname for element in constraint.elements}
        == {"approval_cases.workspace_id", "approval_cases.id"}
        for constraint in approval_step.foreign_key_constraints
    )


def test_each_workflow_table_names_its_own_status() -> None:
    """申请、审批流和节点使用各自明确的状态列名。"""

    tables = load_tables()

    assert "request_status" in tables["access_requests"].c
    assert "approval_status" in tables["approval_cases"].c
    assert "step_status" in tables["approval_steps"].c
