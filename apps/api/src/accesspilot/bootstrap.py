"""新数据库的迁移后目录与政策向量初始化。"""

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.embeddings import (
    DashScopeEmbeddingModel,
    DeterministicEmbeddingModel,
    EmbeddingModel,
)
from accesspilot.config import Settings
from accesspilot.db.seed import seed_catalog
from accesspilot.db.session import build_engine, build_session_factory
from accesspilot.rag.policies import index_policy_embeddings


@dataclass(frozen=True)
class BootstrapResult:
    """可安全输出的初始化结果，不包含密钥或向量正文。"""

    policy_count: int
    embedding_mode: str


def bootstrap_database(
    session_factory: sessionmaker[Session],
    *,
    embedding_model: EmbeddingModel,
    embedding_mode: str,
) -> BootstrapResult:
    """幂等写入虚构目录，并用当前适配器重建同源政策向量。"""

    with session_factory() as session:
        seed_catalog(session)
        policy_count = index_policy_embeddings(session, embedding_model)
    return BootstrapResult(
        policy_count=policy_count,
        embedding_mode=embedding_mode,
    )


def _configured_embedding(settings: Settings) -> tuple[EmbeddingModel, str]:
    if settings.dashscope_api_key is None:
        return DeterministicEmbeddingModel(), "deterministic-offline"
    return (
        DashScopeEmbeddingModel(
            api_key=settings.dashscope_api_key.get_secret_value(),
            model_name=settings.dashscope_embedding_model,
            base_url=settings.dashscope_base_url,
        ),
        settings.dashscope_embedding_model,
    )


def main() -> None:
    """供本地与容器启动脚本调用；日志只报告非敏感初始化摘要。"""

    settings = Settings()
    model, mode = _configured_embedding(settings)
    result = bootstrap_database(
        build_session_factory(build_engine(settings.database_url)),
        embedding_model=model,
        embedding_mode=mode,
    )
    print(
        "AccessPilot catalog ready: "
        f"{result.policy_count} policy chunks, embedding={result.embedding_mode}"
    )


if __name__ == "__main__":
    main()
