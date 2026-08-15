"""Workspace 经过 FastAPI 重启后的持久化集成测试。"""

import os

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.config import Settings
from accesspilot.db.seed import seed_catalog
from accesspilot.main import create_app
from support.auth import login_as


def test_workspace_draft_survives_new_app_instance(
    database_session_factory: sessionmaker[Session],
) -> None:
    """用新建的 FastAPI 实例模拟进程重启，原 Cookie 仍能读取草稿。"""

    # 不注入 Store，确认实际默认启动路径使用 PostgreSQL。
    settings = Settings(
        database_url=os.getenv(
            "ACCESSPILOT_TEST_DATABASE_URL",
            "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test",
        ),
        demo_mode_enabled=True,
    )
    with database_session_factory() as session:
        seed_catalog(session)
    first_app = create_app(settings=settings, session_factory=database_session_factory)
    with TestClient(first_app) as first_browser:
        login_as(first_browser)
        preview = first_browser.post(
            "/api/drafts/preview",
            json={
                "entitlement_id": "insighthub.customer_export",
                "duration_days": 14,
                "justification": "核验项目运营数据",
                "confirmed": False,
            },
        )
        assert preview.status_code == 200
        token = first_browser.cookies.get("accesspilot_session")

    assert token is not None

    # 重新创建 Store 和 FastAPI 实例，不复用任何内存对象。
    restarted_app = create_app(settings=settings, session_factory=database_session_factory)
    with TestClient(restarted_app) as restarted_browser:
        restarted_browser.cookies.set("accesspilot_session", token)
        response = restarted_browser.get("/api/drafts/current")

    assert response.status_code == 200
    assert response.json()["draft"] == {
        "employee_id": "EMP-001",
        "entitlement_id": "insighthub.customer_export",
        "duration_days": 14,
        "justification": "核验项目运营数据",
        "confirmed": False,
    }
