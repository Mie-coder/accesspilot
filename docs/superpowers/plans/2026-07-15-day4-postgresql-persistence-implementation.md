# Day 4 PostgreSQL 持久化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 PostgreSQL 16 持久化 AccessPilot 的 Workspace、虚构目录、申请、审批、授权、审计和政策分块，并证明新 Session 和 API 重启后数据仍存在。

**Architecture:** Pydantic 继续负责 API/Agent 契约，SQLAlchemy 2.0 负责 ORM，Alembic 负责表结构版本。同步 psycopg 连接本机 `55432` 端口的轻量数据库，Workspace Store 通过接口从内存实现替换为数据库实现。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0、Alembic、psycopg 3、PostgreSQL 16、pgvector 0.8.1、pytest、Ruff、MyPy。

## Global Constraints

- 本机使用 `postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot`，不安装 Docker/QEMU。
- 数据目录 `postgres-data/` 和 `.env` 不得进入 Git。
- 所有企业数据必须是原创虚构数据，不得包含美的内部信息。
- Workspace 私有数据必须带 `workspace_id`，原始 Cookie Token 不得存入数据库。
- `AccessRequest`、`ApprovalCase` 和 `AccessGrant` 必须分表。
- `policy_chunks.embedding` 使用 `vector(512)` 且允许为空；首版不创建 ANN 索引。
- 用户写核心 ORM/Store 逻辑；Codex 写测试、运行验证并补充中文注释。

---

### Task 1: 数据库依赖、配置与 Session 工厂

**Files:**
- Modify: `pyproject.toml`
- Modify: `apps/api/src/accesspilot/config.py`
- Create: `.env.example`
- Create: `apps/api/src/accesspilot/db/__init__.py`
- Create: `apps/api/src/accesspilot/db/base.py`
- Create: `apps/api/src/accesspilot/db/session.py`
- Test: `apps/api/tests/db/test_session.py`

**Interfaces:**
- Produces: `Base`、`build_engine(database_url: str) -> Engine`、`build_session_factory(engine: Engine) -> sessionmaker[Session]`。
- Consumes: `Settings.database_url: str`。

- [ ] **Step 1: 增加最小数据库依赖**

```toml
dependencies = [
  "alembic>=1.13,<2",
  "fastapi>=0.115,<1",
  "pgvector>=0.3,<1",
  "psycopg[binary]>=3.2,<4",
  "pydantic>=2.8,<3",
  "pydantic-settings>=2.4,<3",
  "sqlalchemy>=2.0,<3",
]
```

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/pip install -e '.[dev]'`

Expected: 安装成功，`pip check` 输出 `No broken requirements found.`

- [ ] **Step 2: Codex 写 Session 工厂失败测试**

```python
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.session import build_engine, build_session_factory


def test_builds_sync_postgresql_session_factory() -> None:
    engine = build_engine(
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
    )
    factory = build_session_factory(engine)

    assert isinstance(engine, Engine)
    assert isinstance(factory, sessionmaker)
    assert factory.class_ is Session
```

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/db/test_session.py -v`

Expected: FAIL，提示 `accesspilot.db` 或 `build_engine` 不存在。

- [ ] **Step 3: 用户实现 Base 和 Session 工厂**

```python
# apps/api/src/accesspilot/db/base.py
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

```python
# apps/api/src/accesspilot/db/session.py
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def build_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
```

在 `Settings` 中增加：

```python
database_url: str = (
    "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
)
```

`.env.example` 写入：

```dotenv
ACCESSPILOT_DATABASE_URL=postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot
```

- [ ] **Step 4: 验证 Session 任务**

Run:

```bash
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/db/test_session.py -v
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/ruff check apps/api/src apps/api/tests
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/mypy apps/api/src
```

Expected: 新测试 PASS，Ruff 和 MyPy 均无错误。

- [ ] **Step 5: 提交 Task 1**

```bash
git add pyproject.toml .env.example apps/api/src/accesspilot/config.py \
  apps/api/src/accesspilot/db apps/api/tests/db/test_session.py
git commit -m "feat: 添加数据库连接基础"
```

### Task 2: ORM 表与首版 Alembic 迁移

**Files:**
- Create: `apps/api/src/accesspilot/db/models.py`
- Create: `alembic.ini`
- Create: `apps/api/migrations/env.py`
- Create: `apps/api/migrations/script.py.mako`
- Create: `apps/api/migrations/versions/20260715_0001_create_initial_schema.py`
- Test: `apps/api/tests/db/test_models.py`

**Interfaces:**
- Consumes: `accesspilot.db.base.Base`。
- Produces: `WorkspaceRecord`、`EmployeeRecord`、`SystemRecord`、`EntitlementRecord`、`AccessRequestRecord`、`ApprovalCaseRecord`、`ApprovalStepRecord`、`AccessGrantRecord`、`AuditEventRecord`、`PolicyChunkRecord`。

- [ ] **Step 1: Codex 写元数据失败测试**

```python
from accesspilot.db.base import Base
from accesspilot.db import models  # noqa: F401


def test_metadata_contains_day4_tables() -> None:
    assert set(Base.metadata.tables) == {
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


def test_idempotency_and_approval_order_are_unique() -> None:
    grant = Base.metadata.tables["access_grants"]
    step = Base.metadata.tables["approval_steps"]

    assert grant.c.idempotency_key.unique is True
    assert any(
        set(constraint.columns.keys()) == {"approval_case_id", "step_order"}
        for constraint in step.constraints
    )
```

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/db/test_models.py -v`

Expected: FAIL，提示 `accesspilot.db.models` 不存在。

- [ ] **Step 2: 用户建立公共字段写法**

`models.py` 使用以下导入和时间工具：

```python
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from accesspilot.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)
```

- [ ] **Step 3: 用户先实现 Workspace 与全局虚构目录表**

```python
class WorkspaceRecord(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fault_mode: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class EmployeeRecord(Base):
    __tablename__ = "employees"

    employee_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    department: Mapped[str] = mapped_column(String(100), index=True)
    manager_id: Mapped[str | None] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="SET NULL"), nullable=True
    )
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(50)), default=list)


class SystemRecord(Base):
    __tablename__ = "systems"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class EntitlementRecord(Base):
    __tablename__ = "entitlements"

    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    system_code: Mapped[str] = mapped_column(
        ForeignKey("systems.code", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    risk_level: Mapped[str] = mapped_column(String(20))
    approval_policy: Mapped[str] = mapped_column(String(50))
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="SET NULL"), nullable=True
    )
    self_service_allowed: Mapped[bool] = mapped_column(Boolean)
    eligible_departments: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)), default=list
    )
    eligible_roles: Mapped[list[str]] = mapped_column(ARRAY(String(50)), default=list)
    max_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: 用户实现申请、审批、授权与审计表**

```python
class AccessRequestRecord(Base):
    __tablename__ = "access_requests"
    __table_args__ = (
        CheckConstraint("duration_days > 0", name="duration_days_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="RESTRICT")
    )
    entitlement_code: Mapped[str] = mapped_column(
        ForeignKey("entitlements.code", ondelete="RESTRICT")
    )
    project_code: Mapped[str] = mapped_column(String(100))
    data_scope: Mapped[str] = mapped_column(Text)
    business_reason: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    duration_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ApprovalCaseRecord(Base):
    __tablename__ = "approval_cases"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("access_requests.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class ApprovalStepRecord(Base):
    __tablename__ = "approval_steps"
    __table_args__ = (
        UniqueConstraint("approval_case_id", "step_order"),
        CheckConstraint("step_order > 0", name="step_order_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    approval_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("approval_cases.id", ondelete="CASCADE"), index=True
    )
    step_order: Mapped[int] = mapped_column(Integer)
    approver_id: Mapped[str] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="RESTRICT")
    )
    approver_role: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class AccessGrantRecord(Base):
    __tablename__ = "access_grants"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("access_requests.id", ondelete="RESTRICT"), unique=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    grantee_id: Mapped[str] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="RESTRICT")
    )
    entitlement_code: Mapped[str] = mapped_column(
        ForeignKey("entitlements.code", ondelete="RESTRICT")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("access_requests.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("employees.employee_id", ondelete="SET NULL"), nullable=True
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
```

- [ ] **Step 5: 用户实现政策分块表**

```python
class PolicyChunkRecord(Base):
    __tablename__ = "policy_chunks"
    __table_args__ = (UniqueConstraint("policy_code", "chunk_index"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    policy_code: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(200))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(512), nullable=True
    )
```

- [ ] **Step 6: 验证 ORM 元数据测试变绿**

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/db/test_models.py -v`

Expected: 2 tests PASS。

- [ ] **Step 7: 初始化 Alembic 并生成固定版本号迁移**

Run:

```bash
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/alembic init apps/api/migrations
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/alembic revision \
  --autogenerate --rev-id 20260715_0001 -m "create initial schema"
```

`apps/api/migrations/env.py` 必须导入 `Base` 和 `accesspilot.db.models`，并设置：

```python
from accesspilot.config import Settings
from accesspilot.db import models  # noqa: F401
from accesspilot.db.base import Base

config.set_main_option("sqlalchemy.url", Settings().database_url)
target_metadata = Base.metadata
```

在生成的 `upgrade()` 第一行表操作前加入：

```python
op.execute("CREATE EXTENSION IF NOT EXISTS vector")
```

在 `downgrade()` 删除 `policy_chunks` 等表后最后加入：

```python
op.execute("DROP EXTENSION IF EXISTS vector")
```

- [ ] **Step 8: 用空测试数据库验证迁移**

Run:

```bash
/Library/PostgreSQL/16/bin/dropdb -p 55432 -U accesspilot --if-exists accesspilot_test
/Library/PostgreSQL/16/bin/createdb -p 55432 -U accesspilot accesspilot_test
ACCESSPILOT_DATABASE_URL=postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test \
  LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/alembic upgrade head
/Library/PostgreSQL/16/bin/psql -p 55432 -U accesspilot -d accesspilot_test \
  -c '\dt' -c '\dx vector'
```

Expected: 10 张业务表、`alembic_version` 和 `vector 0.8.1` 可见。

- [ ] **Step 9: 提交 Task 2**

```bash
git add alembic.ini apps/api/migrations apps/api/src/accesspilot/db/models.py \
  apps/api/tests/db/test_models.py
git commit -m "feat: 建立权限申请持久化模型"
```

### Task 3: 可重复执行的虚构种子数据

**Files:**
- Modify: `apps/api/src/accesspilot/domain/catalog.py`
- Create: `apps/api/src/accesspilot/db/seed.py`
- Create: `apps/api/tests/db/conftest.py`
- Test: `apps/api/tests/db/test_seed.py`

**Interfaces:**
- Consumes: `EMPLOYEES`、`ENTITLEMENTS`、`POLICIES`和 ORM 目录表。
- Produces: `SYSTEMS: dict[str, System]`、`seed_catalog(session: Session) -> None`和真实数据库 pytest fixtures。

- [ ] **Step 1: Codex 建立真实 PostgreSQL 测试 fixtures**

```python
import os
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.session import build_engine, build_session_factory


@pytest.fixture(scope="session")
def database_session_factory() -> sessionmaker[Session]:
    url = os.getenv(
        "ACCESSPILOT_TEST_DATABASE_URL",
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test",
    )
    return build_session_factory(build_engine(url))


@pytest.fixture
def database_session(
    database_session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    with database_session_factory() as session:
        yield session
```

- [ ] **Step 2: Codex 写种子数据失败测试**

```python
from sqlalchemy import func, select

from accesspilot.db.models import EmployeeRecord, EntitlementRecord, PolicyChunkRecord
from accesspilot.db.seed import seed_catalog


def test_seed_catalog_is_idempotent(database_session) -> None:
    seed_catalog(database_session)
    seed_catalog(database_session)

    assert (
        database_session.scalar(select(func.count()).select_from(EmployeeRecord)) == 5
    )
    assert (
        database_session.scalar(select(func.count()).select_from(EntitlementRecord)) == 7
    )
    assert (
        database_session.scalar(select(func.count()).select_from(PolicyChunkRecord)) == 8
    )
```

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/db/test_seed.py -v`

Expected: FAIL，提示 `seed_catalog` 不存在。

- [ ] **Step 3: 用户为虚构系统建立领域目录**

```python
@dataclass(frozen=True)
class System:
    code: str
    name: str


SYSTEMS = {
    "insighthub": System(code="insighthub", name="InsightHub 数据洞察中心"),
    "codeforge": System(code="codeforge", name="CodeForge 代码协作平台"),
    "opsdesk": System(code="opsdesk", name="OpsDesk 运维工作台"),
}
```

- [ ] **Step 4: 用户实现幂等 `seed_catalog`**

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.domain.catalog import EMPLOYEES, ENTITLEMENTS, POLICIES, SYSTEMS
from accesspilot.db.models import (
    EmployeeRecord,
    EntitlementRecord,
    PolicyChunkRecord,
    SystemRecord,
)


def seed_catalog(session: Session) -> None:
    employees: dict[str, EmployeeRecord] = {}
    for employee in EMPLOYEES.values():
        employees[employee.employee_id] = session.merge(
            EmployeeRecord(
                employee_id=employee.employee_id,
                name=employee.name,
                department=employee.department,
                manager_id=None,
                roles=sorted(employee.roles),
            )
        )
    session.flush()

    for employee in EMPLOYEES.values():
        employees[employee.employee_id].manager_id = employee.manager_id

    for system in SYSTEMS.values():
        session.merge(SystemRecord(code=system.code, name=system.name))
    session.flush()

    for entitlement in ENTITLEMENTS.values():
        session.merge(
            EntitlementRecord(
                code=entitlement.code,
                system_code=entitlement.system_code,
                name=entitlement.name,
                risk_level=entitlement.risk_level.value,
                approval_policy=entitlement.approval_policy.value,
                owner_id=entitlement.owner_id,
                self_service_allowed=entitlement.self_service_allowed,
                eligible_departments=sorted(entitlement.eligible_departments),
                eligible_roles=sorted(entitlement.eligible_roles),
                max_duration_days=entitlement.max_duration_days,
            )
        )

    for policy in POLICIES:
        chunk = session.scalar(
            select(PolicyChunkRecord).where(
                PolicyChunkRecord.policy_code == policy.code,
                PolicyChunkRecord.chunk_index == 0,
            )
        )
        if chunk is None:
            chunk = PolicyChunkRecord(
                policy_code=policy.code,
                title=policy.title,
                chunk_index=0,
                content=policy.content,
                chunk_metadata={"source": "fictional_access_policy"},
                embedding=None,
            )
            session.add(chunk)
        else:
            chunk.title = policy.title
            chunk.content = policy.content
            chunk.chunk_metadata = {"source": "fictional_access_policy"}
    session.commit()
```

- [ ] **Step 5: 验证种子数据两次加载后数量不变**

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/db/test_seed.py -v`

Expected: PASS，员工 5、权限 7、政策分块 8。

- [ ] **Step 6: 提交 Task 3**

```bash
git add apps/api/src/accesspilot/domain/catalog.py apps/api/src/accesspilot/db/seed.py \
  apps/api/tests/db/conftest.py apps/api/tests/db/test_seed.py
git commit -m "feat: 加载虚构权限目录数据"
```

### Task 4: 新 Session 读取申请与审计记录

**Files:**
- Create: `apps/api/tests/db/test_persistence.py`

**Interfaces:**
- Consumes: `database_session_factory` fixture 和 Day 4 ORM 模型。
- Produces: 跨 Session 持久化证据。

- [ ] **Step 1: Codex 写跨 Session 集成测试**

```python
from datetime import UTC, date, datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from accesspilot.db.models import (
    AccessRequestRecord,
    AuditEventRecord,
    WorkspaceRecord,
)
from accesspilot.db.seed import seed_catalog


def test_new_session_reads_committed_request_and_audit(
    database_session_factory,
) -> None:
    with database_session_factory() as first:
        seed_catalog(first)
        token_hash = sha256(uuid4().bytes).hexdigest()
        workspace = WorkspaceRecord(token_hash=token_hash)
        first.add(workspace)
        first.flush()
        request = AccessRequestRecord(
            workspace_id=workspace.id,
            requester_id="EMP-001",
            entitlement_code="insighthub.customer_export",
            project_code="PRJ-AURORA",
            data_scope="华南区脱敏客户数据",
            business_reason="核验项目运营数据",
            start_date=date(2026, 7, 15),
            duration_days=14,
            status="submitted",
            confirmed_at=datetime.now(UTC),
        )
        first.add(request)
        first.flush()
        first.add(
            AuditEventRecord(
                workspace_id=workspace.id,
                request_id=request.id,
                event_type="request.submitted",
                actor_id="EMP-001",
                summary={"project_code": "PRJ-AURORA"},
            )
        )
        request_id = request.id
        first.commit()

    with database_session_factory() as second:
        stored_request = second.get(AccessRequestRecord, request_id)
        stored_audit = second.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.request_id == request_id
            )
        )

        assert stored_request is not None
        assert stored_request.project_code == "PRJ-AURORA"
        assert stored_audit is not None
        assert stored_audit.event_type == "request.submitted"
```

- [ ] **Step 2: 运行真实数据库集成测试**

Run:

```bash
ACCESSPILOT_TEST_DATABASE_URL=postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test \
  LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
  .venv/bin/python -m pytest apps/api/tests/db/test_persistence.py -v
```

Expected: PASS，第二个 Session 读到第一个 Session 已 commit 的申请和审计记录。

- [ ] **Step 3: 提交 Task 4**

```bash
git add apps/api/tests/db/test_persistence.py
git commit -m "test: 验证申请与审计跨会话持久化"
```

### Task 5: 持久化 Workspace 与 API 重启验证

**Files:**
- Create: `apps/api/src/accesspilot/db/workspace_store.py`
- Modify: `apps/api/src/accesspilot/main.py`
- Test: `apps/api/tests/api/test_workspace_persistence.py`

**Interfaces:**
- Consumes: `WorkspaceStore` Protocol、`WorkspaceRecord` 和 `sessionmaker[Session]`。
- Produces: `SqlAlchemyWorkspaceStore`。

- [ ] **Step 1: Codex 写 API 重启失败测试**

```python
from fastapi.testclient import TestClient

from accesspilot.db.workspace_store import SqlAlchemyWorkspaceStore
from accesspilot.main import create_app


def test_workspace_survives_new_app_instance(database_session_factory) -> None:
    first_app = create_app(store=SqlAlchemyWorkspaceStore(database_session_factory))
    with TestClient(first_app) as first_client:
        created = first_client.post("/api/workspaces")
        cookie = created.cookies.get("accesspilot_workspace")
        assert cookie is not None
        first_client.post(
            "/api/drafts/preview",
            json={"system_name": "InsightHub", "entitlement_name": "客户数据导出"},
        ).raise_for_status()

    second_app = create_app(store=SqlAlchemyWorkspaceStore(database_session_factory))
    with TestClient(second_app) as second_client:
        second_client.cookies.set("accesspilot_workspace", cookie)
        response = second_client.post("/api/workspaces/reset")

    assert response.status_code == 200
    assert response.json() == {"status": "reset"}
```

Run: `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests/api/test_workspace_persistence.py -v`

Expected: FAIL，提示 `SqlAlchemyWorkspaceStore` 不存在。

- [ ] **Step 2: 用户实现 Token 哈希和 ORM Store**

```python
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import WorkspaceRecord
from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import Workspace


def hash_workspace_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


class SqlAlchemyWorkspaceStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, token: str) -> Workspace | None:
        token_hash = hash_workspace_token(token)
        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == token_hash
                )
            )
            if record is None:
                return None
            draft = (
                RequestDraft.model_validate(record.draft)
                if record.draft is not None
                else None
            )
            return Workspace(
                token=token,
                draft=draft,
                fault_mode=record.fault_mode,
            )

    def save(self, workspace: Workspace) -> None:
        token_hash = hash_workspace_token(workspace.token)
        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == token_hash
                )
            )
            if record is None:
                record = WorkspaceRecord(token_hash=token_hash)
                session.add(record)
            record.draft = (
                workspace.draft.model_dump(mode="json")
                if workspace.draft is not None
                else None
            )
            record.fault_mode = workspace.fault_mode
            session.commit()
```

- [ ] **Step 3: 让 FastAPI 默认使用数据库 Store**

`create_app()` 保留测试注入 `store` 的能力。当 `store is None` 时，根据 `active_settings.database_url` 创建 Engine、Session Factory 和 `SqlAlchemyWorkspaceStore`；传入 Store 时不建立默认数据库 Store。

```python
if store is None:
    engine = build_engine(active_settings.database_url)
    session_factory = build_session_factory(engine)
    active_store: WorkspaceStore = SqlAlchemyWorkspaceStore(session_factory)
else:
    active_store = store

workspace_service = WorkspaceService(active_store)
```

- [ ] **Step 4: 验证新 App 实例能读取原 Workspace**

Run:

```bash
ACCESSPILOT_TEST_DATABASE_URL=postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test \
  LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
  .venv/bin/python -m pytest apps/api/tests/api/test_workspace_persistence.py -v
```

Expected: PASS。数据库中只能查到 64 位 Token 哈希，不得出现 Cookie 原文。

- [ ] **Step 5: 提交 Task 5**

```bash
git add apps/api/src/accesspilot/db/workspace_store.py apps/api/src/accesspilot/main.py \
  apps/api/tests/api/test_workspace_persistence.py
git commit -m "feat: 持久化演示工作区"
```

### Task 6: Day 4 全量验证与学习记录

**Files:**
- Modify: `docs/superpowers/plans/2026-07-12-accesspilot-learning-schedule.md`
- Modify: `CONTEXT.md`

**Interfaces:**
- Consumes: Task 1–5 的实现与测试证据。
- Produces: Day 4 检查点勾选、术语表更新和可回放提交。

- [ ] **Step 1: 重启 AccessPilot 数据库并确认数据仍在**

Run:

```bash
/Library/PostgreSQL/16/bin/pg_ctl -D postgres-data restart -m fast \
  -o '-p 55432' -l postgres-data/server.log
/Library/PostgreSQL/16/bin/psql -p 55432 -U accesspilot -d accesspilot \
  -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

Expected: PostgreSQL 重启成功，`extversion` 为 `0.8.1`。

- [ ] **Step 2: 运行完整验证组合**

```bash
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/python -m pytest apps/api/tests -q
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/ruff check apps/api/src apps/api/tests
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/mypy apps/api/src
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 .venv/bin/pip check
git diff --check
```

Expected: 全部退出码为 0，pytest 无失败。

- [ ] **Step 3: 只根据证据勾选 Day 4 后四项**

确认 ORM/迁移、种子数据、跨 Session 测试和重启测试均有对应输出后，将 Day 4 剩余 `[ ]` 改为 `[x]`。

- [ ] **Step 4: 更新术语表**

在 `CONTEXT.md` 增加 `ORM`、`Session`、`Transaction`、`Migration`、`Foreign Key`、`pgvector` 的中文解释，并记录“本地轻量 PostgreSQL，云端 Docker Compose”运行差异。

- [ ] **Step 5: 完成 Day 4 提交**

```bash
git add docs/superpowers/plans/2026-07-12-accesspilot-learning-schedule.md CONTEXT.md
git commit -m "docs: 完成 Day 4 持久化学习记录"
```
