"""AccessPilot 的 SQLAlchemy ORM 数据表模型。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from accesspilot.db.base import Base


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，避免不同服务器时区导致歧义。"""

    return datetime.now(UTC)


class WorkspaceRecord(Base):
    """保存一个访客演示空间的可变状态。

    类继承 Base 后，SQLAlchemy 会把它注册到 Base.metadata，
    以后 Alembic 就能根据这份元数据生成建表迁移。
    """

    # Python 中的 WorkspaceRecord 对应 PostgreSQL 中的 workspaces 表。
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "model_call_limit >= 0",
            name="model_call_limit_non_negative",
        ),
        CheckConstraint(
            "model_calls_used >= 0 AND model_calls_used <= model_call_limit",
            name="model_calls_within_limit",
        ),
        CheckConstraint(
            "model_retry_consumed >= 0",
            name="model_retry_consumed_non_negative",
        ),
        CheckConstraint(
            "model_retry_consumed <= model_calls_used",
            name="model_retry_within_calls",
        ),
        CheckConstraint(
            (
                "(demo_session_active AND demo_actor_id IS NOT NULL) OR "
                "(NOT demo_session_active AND demo_actor_id IS NULL)"
            ),
            name="demo_session_actor_consistent",
        ),
        CheckConstraint(
            "cursor_expected_field IS NULL OR cursor_auth_session_id IS NOT NULL",
            name="cursor_auth_session_required",
        ),
        CheckConstraint(
            "flow_version IN (1, 2)",
            name="flow_version_valid",
        ),
        CheckConstraint(
            "lease_fence >= 0",
            name="lease_fence_non_negative",
        ),
        UniqueConstraint("id", "agent_thread_id"),
    )
    # Mapped[UUID] 是 Python 侧类型；Uuid 是数据库列类型。
    # default=uuid4 传入的是函数，SQLAlchemy 会在每条新记录创建时调用它。
    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
    )
    # Agent runtime identity is generated inside the Workspace creation
    # transaction and never crosses the public API/domain DTO boundary.
    agent_thread_id: Mapped[UUID] = mapped_column(
        Uuid,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
        unique=True,
        nullable=False,
    )
    flow_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    lease_fence: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    # 数据库只保存 Cookie Token 的 SHA-256 十六进制哈希，长度固定为 64。
    # unique 防止重复，index 加快每次请求按 Token 哈希查找 Workspace。
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )
    # 演示身份与 Workspace 绑定；不加外键，避免迁移早于目录 seed 时失败。
    actor_id: Mapped[str] = mapped_column(
        String(30),
        default="EMP-001",
        server_default="EMP-001",
        index=True,
    )
    # Demo 覆盖与会话状态分开保存，退出 Demo 不会改绑历史业务事实。
    demo_actor_id: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )
    demo_session_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    # 草稿是对话中可反复修改的结构，序列化成 JSONB 保存。
    # 新 Workspace 可能还没有草稿，所以 Python 类型包含 None，数据库也允许 NULL。
    draft: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    # 每个 Workspace 可以独立注入 IAM 超时等故障；正常模式下没有值。
    fault_mode: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    # 模型配额属于 Workspace，使用行锁原子消费；历史读取不消耗配额。
    model_call_limit: Mapped[int] = mapped_column(
        Integer,
        default=20,
        server_default="20",
    )
    model_calls_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    # 结构化回复纠正重试单独计数，不能从总调用次数推断。
    model_retry_consumed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    # timezone=True 要求保存带时区的时间；传入 utc_now 而不是 utc_now()，
    # 保证每条记录插入时才生成当时的时间。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    # 对话草稿的单调 revision；T18 的纯数字续答通过它做 CAS 绑定。
    draft_revision: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    # ConversationCursor 与 Workspace 同行保存，避免产生跨 Workspace 的
    # 可猜测游标；活动 Cursor 必须带当前 AuthSession。
    cursor_actor_id: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )
    cursor_auth_session_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    cursor_expected_field: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    cursor_last_question_kind: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
    )
    cursor_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cursor_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AuthSessionRecord(Base):
    """Server-owned Mock Login session.

    The browser only receives the random bearer/CSRF values.  Database rows
    contain SHA-256 hashes, so a database read never yields a reusable token.
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        # Runtime ledgers use the three columns together so a caller cannot
        # splice an actor or AuthSession from another Workspace.
        UniqueConstraint("workspace_id", "id", "employee_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
        nullable=False,
    )
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EmployeeRecord(Base):
    """保存原创虚构员工及其直属上级关系。"""

    __tablename__ = "employees"

    # 员工编号是虚构员工目录中的稳定业务标识，例如 EMP-001。
    employee_id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(100))

    # 部门经常用于权限资格过滤，因此建立普通查询索引。
    department: Mapped[str] = mapped_column(
        String(100),
        index=True,
    )

    # 这是自关联外键：manager_id 仍然指向 employees 表中的另一名员工。
    # 最高负责人没有直属上级，因此这个字段允许为空。
    manager_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "employees.employee_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    # PostgreSQL ARRAY 将多个角色保存为字符串数组。
    # default=list 让每条新记录得到自己独立的空列表。
    roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        default=list,
    )


class SystemRecord(Base):
    """保存可申请权限的原创虚构企业系统。"""

    __tablename__ = "systems"

    # 系统编码是稳定业务标识，权限表会通过它建立外键。
    code: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    # 演示目录不允许出现两个同名系统。
    name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
    )


class EntitlementRecord(Base):
    """保存用户能够申请的最小权限单位。"""

    __tablename__ = "entitlements"

    # 权限编码是稳定主键，例如 insighthub.customer_export。
    code: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )

    # 每个权限必须属于一个已经存在的系统。
    # RESTRICT 表示系统仍有权限时，不允许删除这个系统。
    system_code: Mapped[str] = mapped_column(
        ForeignKey(
            "systems.code",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100))
    risk_level: Mapped[str] = mapped_column(String(20))
    approval_policy: Mapped[str] = mapped_column(String(50))

    # owner_id 指向员工表中的数据所有者。
    # 暂时没有所有者时允许为空；员工删除后也将它设为 NULL。
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "employees.employee_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    # 安全规则必须明确为允许或禁止，不能出现“不知道”的 NULL 状态。
    self_service_allowed: Mapped[bool] = mapped_column(Boolean)

    # 空列表表示没有符合条件的部门或角色。
    eligible_departments: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)),
        default=list,
    )
    eligible_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        default=list,
    )

    # None 表示目录没有设置额外的最长期限。
    max_duration_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )


class AccessRequestRecord(Base):
    """保存用户确认后的正式权限申请。

    草稿会反复修改，而这里保存审批人当时看到的申请事实。
    重新申请应创建新记录，不覆盖旧申请的审计历史。
    """

    __tablename__ = "access_requests"
    __table_args__ = (
        # 子表需要同时引用 Workspace 和 Request，目标字段组必须唯一。
        UniqueConstraint("workspace_id", "id"),
        # 数据库作为最后一道防线，拒绝零天或负数期限。
        CheckConstraint(
            "duration_days > 0",
            name="duration_days_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="RESTRICT")
    )
    entitlement_code: Mapped[str] = mapped_column(
        ForeignKey("entitlements.code", ondelete="RESTRICT")
    )

    # 以下是用户确认时冻结的业务内容。
    duration_days: Mapped[int] = mapped_column(Integer)
    justification: Mapped[str] = mapped_column(Text)
    request_status: Mapped[str] = mapped_column(String(40))

    # confirmed_at 记录用户动作；created_at 记录 ORM 创建记录的时间。
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class DecisionPacketRecord(Base):
    """One immutable, source-labelled decision snapshot per formal request."""

    __tablename__ = "decision_packets"
    __table_args__ = (
        CheckConstraint(
            "generation_mode IN ('provider', 'deterministic', 'unavailable')",
            name="generation_mode_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("access_requests.id", ondelete="CASCADE"),
        unique=True,
    )
    packet_version: Mapped[str] = mapped_column(String(30))
    generation_mode: Mapped[str] = mapped_column(String(30))
    catalog_version: Mapped[str] = mapped_column(String(50))
    frozen_content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class ApprovalCaseRecord(Base):
    """保存一份申请的整体审批流程状态。"""

    __tablename__ = "approval_cases"
    __table_args__ = (
        # ApprovalStep 将通过 (workspace_id, approval_case_id) 引用它。
        UniqueConstraint("workspace_id", "id"),
        # 两个字段必须同时匹配同一条申请，阻止跨 Workspace 引用。
        ForeignKeyConstraint(
            ["workspace_id", "request_id"],
            ["access_requests.workspace_id", "access_requests.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    # unique=True 保证一份正式申请最多只有一个审批流。
    request_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    approval_status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class ApprovalStepRecord(Base):
    """保存审批流中一个有顺序、有明确审批人的节点。"""

    __tablename__ = "approval_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "approval_case_id"],
            ["approval_cases.workspace_id", "approval_cases.id"],
            ondelete="CASCADE",
        ),
        # 同一审批流不能出现两个“第 1 步”。
        UniqueConstraint("approval_case_id", "step_order"),
        CheckConstraint("step_order > 0", name="step_order_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    approval_case_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    step_order: Mapped[int] = mapped_column(Integer)
    approver_id: Mapped[str] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="RESTRICT")
    )
    approver_role: Mapped[str] = mapped_column(String(50))
    step_status: Mapped[str] = mapped_column(String(30))

    # Pending 节点还没有意见和决策时间，因此两者允许为空。
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class AccessGrantRecord(Base):
    """只在 IAM 真正开通成功后创建的授权事实。"""

    __tablename__ = "access_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "request_id"],
            ["access_requests.workspace_id", "access_requests.id"],
            ondelete="RESTRICT",
        ),
        # 失效时间必须晚于生效时间，防止产生无效授权区间。
        CheckConstraint("expires_at > starts_at", name="valid_time_range"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    # request_id 唯一：一份申请最多只产生一条真实授权。
    request_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    # IAM 重试时必须重用同一个幂等键，不得把重试当成新操作。
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class ProvisioningAttemptRecord(Base):
    """保存一次可恢复、可幂等重放的 IAM 开通操作。"""

    __tablename__ = "provisioning_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "request_id"],
            ["access_requests.workspace_id", "access_requests.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("attempt_count > 0", name="attempt_count_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    # 每份申请只有一个稳定操作；失败重试也必须复用同一条记录。
    request_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    provisioning_status: Mapped[str] = mapped_column(String(30))
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class AuditEventRecord(Base):
    """保存只追加的业务时间线事件。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        # request_id 允许为空；它有值时必须与 Workspace 一起匹配真实申请。
        ForeignKeyConstraint(
            ["workspace_id", "request_id"],
            ["access_requests.workspace_id", "access_requests.id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    # Workspace reset 等事件不属于某份申请，所以 request_id 可以为空。
    request_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    # actor_type 区分 employee、agent 和 system；系统自动事件可没有 actor_id。
    actor_type: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )


class PolicyChunkRecord(Base):
    """保存 RAG 检索使用的虚构政策分块和向量。"""

    __tablename__ = "policy_chunks"
    __table_args__ = (
        # 同一政策中的分块序号不能重复。
        UniqueConstraint("policy_code", "chunk_index"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    policy_code: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(200))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)

    # Base 已经使用 metadata 这个属性名，所以 Python 中叫 chunk_metadata，
    # mapped_column 的第一个参数仍将 PostgreSQL 列名指定为 metadata。
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        default=dict,
    )
    # Embedding API 失败时允许暂时为空，后续可重试补全；检索时不得伪造结果。
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(512),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class WorkspaceEventRecord(Base):
    """保存允许前端回放的安全事件，不包含模型隐藏推理。"""

    __tablename__ = "workspace_events"
    __table_args__ = (
        # Execution locators use composite event FKs to prevent cross-Workspace
        # input/terminal references even though ``id`` is globally unique.
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint(
            "event_key IS NULL OR event_key ~ '^evt_[0-9a-f]{64}$'",
            name="event_key_format",
        ),
        Index(
            "uq_workspace_events_workspace_event_key_not_null",
            "workspace_id",
            "event_key",
            unique=True,
            postgresql_where=text("event_key IS NOT NULL"),
        ),
    )

    # 全局递增 ID 可直接作为 SSE Last-Event-ID 游标。
    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    event_key: Mapped[str | None] = mapped_column(String(68), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )


class AgentTurnExecutionRecord(Base):
    """One logical user input and its current fenced execution ownership."""

    __tablename__ = "agent_turn_executions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "graph_run_id", "input_seq"),
        UniqueConstraint("input_turn_id"),
        UniqueConstraint("input_event_id"),
        UniqueConstraint("terminal_event_id"),
        ForeignKeyConstraint(
            ["workspace_id", "auth_session_ref", "actor_id"],
            [
                "auth_sessions.workspace_id",
                "auth_sessions.id",
                "auth_sessions.employee_id",
            ],
            name="fk_agent_turn_executions_auth_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "input_event_id"],
            ["workspace_events.workspace_id", "workspace_events.id"],
            name="fk_agent_turn_executions_input_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "terminal_event_id"],
            ["workspace_events.workspace_id", "workspace_events.id"],
            name="fk_agent_turn_executions_terminal_event",
            ondelete="RESTRICT",
        ),
        CheckConstraint("input_seq >= 0", name="input_seq_non_negative"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("lease_fence >= 1", name="lease_fence_positive"),
        CheckConstraint(
            "engine IN ('legacy', 'langgraph')",
            name="engine_valid",
        ),
        CheckConstraint(
            "status IN ('running', 'waiting_input', 'completed', "
            "'recoverable_error', 'interrupted')",
            name="status_valid",
        ),
        CheckConstraint(
            "checkpoint_ns = ''",
            name="root_checkpoint_namespace",
        ),
        CheckConstraint(
            "checkpoint_thread_id = 'accesspilot:v1.3:' || graph_run_id::text",
            name="checkpoint_thread_for_run",
        ),
        CheckConstraint(
            "accepted_checkpoint_id IS NULL OR length(accepted_checkpoint_id) > 0",
            name="accepted_checkpoint_id_non_empty",
        ),
        CheckConstraint(
            "((status = 'running' AND lease_expires_at IS NOT NULL "
            "AND terminal_event_id IS NULL) OR "
            "(status <> 'running' AND lease_expires_at IS NULL "
            "AND terminal_event_id IS NOT NULL))",
            name="lease_terminal_status_consistent",
        ),
        Index(
            "uq_agent_turn_executions_running_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    graph_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    checkpoint_thread_id: Mapped[str] = mapped_column(String(100), nullable=False)
    input_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    input_turn_id: Mapped[str] = mapped_column(String(120), nullable=False)
    input_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    auth_session_ref: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(30), nullable=False)
    engine: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(200), default="", server_default="", nullable=False
    )
    accepted_checkpoint_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    terminal_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AgentPendingInputRecord(Base):
    """Application projection of one accepted LangGraph input interrupt."""

    __tablename__ = "agent_pending_inputs"
    __table_args__ = (
        UniqueConstraint("pending_input_id"),
        ForeignKeyConstraint(
            ["workspace_id", "agent_thread_id"],
            ["workspaces.id", "workspaces.agent_thread_id"],
            name="fk_agent_pending_inputs_workspace_thread",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "auth_session_ref", "actor_id"],
            [
                "auth_sessions.workspace_id",
                "auth_sessions.id",
                "auth_sessions.employee_id",
            ],
            name="fk_agent_pending_inputs_auth_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind = 'confirmation'", name="kind_valid"),
        CheckConstraint("draft_revision >= 0", name="draft_revision_non_negative"),
        CheckConstraint(
            "resume_input_seq IS NULL OR resume_input_seq >= 0",
            name="resume_input_seq_non_negative",
        ),
        CheckConstraint(
            "engine IN ('legacy', 'langgraph')",
            name="engine_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'resuming', 'resolved', "
            "'abandoned_to_legacy', 'abandoned_conflict')",
            name="status_valid",
        ),
        CheckConstraint(
            "checkpoint_ns = ''",
            name="root_checkpoint_namespace",
        ),
        CheckConstraint(
            "checkpoint_thread_id = 'accesspilot:v1.3:' || graph_run_id::text",
            name="checkpoint_thread_for_run",
        ),
        CheckConstraint(
            "length(accepted_checkpoint_id) > 0",
            name="accepted_checkpoint_id_non_empty",
        ),
        CheckConstraint(
            "((status IN ('active', 'abandoned_to_legacy', 'abandoned_conflict') "
            "AND resume_input_seq IS NULL) OR "
            "(status IN ('resuming', 'resolved') AND resume_input_seq IS NOT NULL))",
            name="resume_sequence_status_consistent",
        ),
        CheckConstraint(
            "((status IN ('abandoned_to_legacy', 'abandoned_conflict') "
            "AND retired_at IS NOT NULL AND retirement_reason IS NOT NULL) OR "
            "(status NOT IN ('abandoned_to_legacy', 'abandoned_conflict') "
            "AND retired_at IS NULL AND retirement_reason IS NULL))",
            name="retirement_status_consistent",
        ),
        CheckConstraint(
            "retirement_reason IS NULL OR length(btrim(retirement_reason)) > 0",
            name="retirement_reason_non_empty",
        ),
        Index(
            "uq_agent_pending_inputs_live_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'resuming')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    agent_thread_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    graph_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    checkpoint_thread_id: Mapped[str] = mapped_column(String(100), nullable=False)
    pending_input_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_session_ref: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(30), nullable=False)
    engine: Mapped[str] = mapped_column(String(20), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(200), default="", server_default="", nullable=False
    )
    accepted_checkpoint_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    resume_input_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retirement_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AgentStepExecutionRecord(Base):
    """Application idempotency fact for one side-effecting logical step."""

    __tablename__ = "agent_step_executions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "operation_id"),
        ForeignKeyConstraint(
            ["workspace_id", "graph_run_id", "input_seq"],
            [
                "agent_turn_executions.workspace_id",
                "agent_turn_executions.graph_run_id",
                "agent_turn_executions.input_seq",
            ],
            name="fk_agent_step_executions_logical_input",
            ondelete="CASCADE",
        ),
        CheckConstraint("input_seq >= 0", name="input_seq_non_negative"),
        CheckConstraint(
            "committed_revision IS NULL OR committed_revision >= 0",
            name="committed_revision_non_negative",
        ),
        CheckConstraint(
            "operation_id ~ '^op_[0-9a-f]{64}$'",
            name="operation_id_format",
        ),
        CheckConstraint(
            "length(btrim(step_key)) > 0",
            name="step_key_non_empty",
        ),
        CheckConstraint(
            "result_reference IS NULL OR length(btrim(result_reference)) > 0",
            name="result_reference_non_empty",
        ),
        CheckConstraint(
            "status IN ('reserved', 'completed')",
            name="status_valid",
        ),
        CheckConstraint(
            "((status = 'reserved' AND result_reference IS NULL "
            "AND committed_revision IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND (NULLIF(btrim(result_reference), '') IS NOT NULL "
            "OR committed_revision IS NOT NULL)))",
            name="completion_facts_consistent",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    graph_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    input_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(67), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # References are deliberately scalar and size bounded: raw provider/tool
    # results never belong in this idempotency ledger.
    result_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    committed_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
