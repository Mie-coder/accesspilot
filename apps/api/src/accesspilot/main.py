"""FastAPI 应用入口"""

from fastapi import FastAPI

from accesspilot.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建一个可配置、可测试的 FastAPI 应用。"""
    active_settings = settings or Settings()
    app = FastAPI(title=active_settings.app_name)

    @app.get('/health')
    def health() -> dict[str,str]:
        """返回最小存活状态，不访问外部依赖"""
        return {"status":"ok"}
        
    return app

app = create_app()