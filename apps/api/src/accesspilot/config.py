""" AccessPilot 应用配置 """

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从默认值或环境变量读取阴影配置"""
    app_name: str = "AccessPilot API"
    workspace_cookie_name:str = "accesspilot_workspace"
    workspace_cookie_secure:bool = False

    model_config = SettingsConfigDict(
        env_prefix="ACCESSPILOT_",
        extra="ignore"
    )
