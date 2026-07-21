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
        )
    )
    first_app = create_app(settings=settings)
    with TestClient(first_app) as first_browser:
        first_browser.post("/api/workspaces")
        first_browser.post(
            "/api/drafts/preview",
            json={
                "system_name": "数据洞察中心",
                "entitlement_name": "客户数据导出",
                "project_code": "PRJ-AURORA",
            },
        )
        token = first_browser.cookies.get("accesspilot_workspace")

    assert token is not None

    # 重新创建 Store 和 FastAPI 实例，不复用任何内存对象。
    restarted_app = create_app(settings=settings)
    with TestClient(restarted_app) as restarted_browser:
        restarted_browser.cookies.set("accesspilot_workspace", token)
        response = restarted_browser.get("/api/drafts/current")

    assert response.status_code == 200
    assert response.json()["draft"]["system_name"] == "数据洞察中心"
    assert response.json()["draft"]["project_code"] == "PRJ-AURORA"
