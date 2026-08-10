"""Workspace 经过 FastAPI 重启后的持久化集成测试。"""

import os

from fastapi.testclient import TestClient

from accesspilot.config import Settings
from accesspilot.main import create_app


def test_workspace_draft_survives_new_app_instance() -> None:
    """用新建的 FastAPI 实例模拟进程重启，原 Cookie 仍能读取草稿。"""

    # 不注入 Store，确认实际默认启动路径使用 PostgreSQL。
    settings = Settings(
        database_url=os.getenv(
            "ACCESSPILOT_TEST_DATABASE_URL",
            "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot_test",
        ),
        demo_mode_enabled=True,
    )
    first_app = create_app(settings=settings)
    with TestClient(first_app) as first_browser:
        assert first_browser.post("/api/workspaces").status_code == 201
        entered = first_browser.post(
            "/api/demo/session",
            json={"employee_id": "EMP-001"},
        )
        assert entered.status_code == 200
        switched = first_browser.post(
            "/api/demo/session",
            json={"employee_id": "EMP-002"},
        )
        assert switched.status_code == 200
        preview = first_browser.post(
            "/api/drafts/preview",
            json={
                # 请求体即使伪造员工编号，草稿也必须绑定后端身份。
                "employee_id": "EMP-999",
                "entitlement_id": "insighthub.customer_export",
                "duration_days": 14,
                "justification": "核验项目运营数据",
                "confirmed": False,
            },
        )
        assert preview.status_code == 200
        token = first_browser.cookies.get("accesspilot_workspace")

    assert token is not None

    # 重新创建 Store 和 FastAPI 实例，不复用任何内存对象。
    restarted_app = create_app(settings=settings)
    with TestClient(restarted_app) as restarted_browser:
        restarted_browser.cookies.set("accesspilot_workspace", token)
        response = restarted_browser.get("/api/drafts/current")
        identity = restarted_browser.get("/api/workspaces/identity")

    assert response.status_code == 200
    assert identity.json()["employee_id"] == "EMP-002"
    assert response.json()["draft"] == {
        "employee_id": "EMP-002",
        "entitlement_id": "insighthub.customer_export",
        "duration_days": 14,
        "justification": "核验项目运营数据",
        "confirmed": False,
    }
