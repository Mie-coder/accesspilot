"""AccessPilot 应用配置。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从默认值或 ACCESSPILOT_ 前缀的环境变量读取配置。"""

    app_name: str = "AccessPilot 权限助手API"
    workspace_cookie_name: str = "accesspilot_workspace"
    workspace_cookie_secure: bool = False
    database_url: str = (
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
    )

    model_config = SettingsConfigDict(
        env_prefix="ACCESSPILOT_",
        extra="ignore",
    )
