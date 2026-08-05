"""应用配置的默认值测试。"""

from accesspilot.config import Settings


def test_database_url_defaults_to_lightweight_local_postgres(
    monkeypatch,
) -> None:
    """旧电脑本地开发默认连接 55432 端口的专用数据库。"""

    monkeypatch.delenv("ACCESSPILOT_DATABASE_URL", raising=False)

    assert Settings().database_url == (
        "postgresql+psycopg://accesspilot@127.0.0.1:55432/accesspilot"
    )


def test_deepseek_settings_use_server_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    settings = Settings()

    assert settings.deepseek_api_key is not None
    assert settings.deepseek_api_key.get_secret_value() == "local-test-key"
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert "local-test-key" not in repr(settings)
