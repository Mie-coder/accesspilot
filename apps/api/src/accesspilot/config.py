"""AccessPilot 应用配置。"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从默认值或 ACCESSPILOT_ 前缀的环境变量读取配置。"""

    product_workspace_cookie_name: str = "accesspilot_product_workspace"
    app_name: str = "AccessPilot 权限助手API"
    workspace_cookie_name: str = "accesspilot_workspace"
    workspace_cookie_secure: bool = False
    product_actor_id: str = "EMP-001"
    demo_mode_enabled: bool = False
    database_url: str = (
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
    )
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="DEEPSEEK_API_KEY",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="DEEPSEEK_MODEL",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="DEEPSEEK_BASE_URL",
    )
    dashscope_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="DASHSCOPE_API_KEY",
    )
    dashscope_embedding_model: str = Field(
        default="text-embedding-v4",
        validation_alias="DASHSCOPE_EMBEDDING_MODEL",
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="DASHSCOPE_BASE_URL",
    )

    model_config = SettingsConfigDict(
        env_prefix="ACCESSPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
