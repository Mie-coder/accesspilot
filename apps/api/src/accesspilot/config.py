"""AccessPilot 应用配置。"""

import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从默认值或 ACCESSPILOT_ 前缀的环境变量读取配置。"""

    product_workspace_cookie_name: str = "accesspilot_product_workspace"
    app_name: str = "AccessPilot 权限助手API"
    workspace_cookie_name: str = "accesspilot_workspace"
    workspace_cookie_secure: bool = False
    # v1.2 Mock Session settings.  The origin is intentionally exact rather
    # than a wildcard; browser requests supply the Origin automatically.
    web_origin: str = "http://127.0.0.1:5173"
    auth_cookie_name: str = "accesspilot_session"
    csrf_cookie_name: str = "accesspilot_csrf"
    auth_cookie_secure: bool = False
    auth_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60)
    product_actor_id: str = "EMP-001"
    demo_mode_enabled: bool = False
    database_url: str = (
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
    )
    # Deploy-time DDL credentials are intentionally separate from both the
    # application and checkpoint runtime credentials.  They are only consumed
    # by ``python -m accesspilot.checkpoint_init``.
    migration_database_url: str | None = None
    checkpoint_migration_database_url: str | None = None
    checkpoint_database_url: str | None = None
    checkpoint_schema: str = "accesspilot_checkpoint"
    checkpoint_pool_min_size: int = Field(default=1, ge=1, le=20)
    checkpoint_pool_max_size: int = Field(default=4, ge=1, le=50)
    checkpoint_readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    orchestrator_mode: Literal["legacy", "mixed", "langgraph"] = "legacy"
    langgraph_canary_percent: int | None = None
    langgraph_strict_msgpack: bool = Field(
        default=False,
        validation_alias="LANGGRAPH_STRICT_MSGPACK",
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
    decision_packet_timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
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
    policy_similarity_threshold: float = Field(default=0.20, ge=0.0, le=1.0)

    @field_validator("checkpoint_schema")
    @classmethod
    def validate_checkpoint_schema(cls, value: str) -> str:
        if value == "public" or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
            raise ValueError("checkpoint_schema must be an independent checkpoint schema")
        return value

    @model_validator(mode="after")
    def validate_orchestrator_configuration(self) -> "Settings":
        if self.checkpoint_pool_min_size > self.checkpoint_pool_max_size:
            raise ValueError("checkpoint_pool_min_size cannot exceed max size")
        if self.orchestrator_mode == "mixed":
            if self.langgraph_canary_percent is None:
                raise ValueError("langgraph_canary_percent is required in mixed mode")
            if not 0 <= self.langgraph_canary_percent <= 100:
                raise ValueError("langgraph_canary_percent must be between 0 and 100")
            if self.langgraph_canary_percent != 0:
                # T35 gate: until the T38/T40 JSON/SSE entry gates pass, mixed
                # must never allocate a real flow 2 Workspace.
                raise ValueError(
                    "langgraph_canary_percent must stay 0 until the JSON/SSE "
                    "entry gates pass"
                )
        elif self.langgraph_canary_percent is not None:
            raise ValueError("langgraph_canary_percent is only valid in mixed mode")
        if self.orchestrator_mode in {"mixed", "langgraph"}:
            if not self.checkpoint_database_url:
                raise ValueError(
                    "checkpoint_database_url is required when LangGraph is allowed"
                )
            if not self.langgraph_strict_msgpack:
                raise ValueError(
                    "LANGGRAPH_STRICT_MSGPACK=true is required when LangGraph is allowed"
                )
        return self

    model_config = SettingsConfigDict(
        env_prefix="ACCESSPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
