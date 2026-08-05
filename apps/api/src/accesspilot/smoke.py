"""真实模型适配器的手动 smoke 入口；不会打印密钥或完整向量。"""

import argparse
from uuid import UUID

from accesspilot.agent.embeddings import DashScopeEmbeddingModel
from accesspilot.config import Settings
from accesspilot.rag.policies import PolicyMatch
from accesspilot.risk.deepseek import DeepSeekRiskReviewModel
from accesspilot.risk.review import RiskReviewContext


def smoke_dashscope_embedding(settings: Settings) -> None:
    """调用一次百炼，并且只输出安全的模型与维度摘要。"""

    if settings.dashscope_api_key is None:
        raise RuntimeError("DASHSCOPE_API_KEY 尚未配置")
    model = DashScopeEmbeddingModel(
        api_key=settings.dashscope_api_key.get_secret_value(),
        model_name=settings.dashscope_embedding_model,
        base_url=settings.dashscope_base_url,
    )
    vector = model.embed(["虚构政策：高风险权限必须经过两级人工审批。"])[0]
    print(
        {
            "provider": "dashscope",
            "model": settings.dashscope_embedding_model,
            "dimensions": len(vector),
        }
    )


def smoke_deepseek_risk(settings: Settings) -> None:
    """调用一次 DeepSeek 风险审查，并只打印经过严格校验的业务 JSON。"""

    if settings.deepseek_api_key is None:
        raise RuntimeError("DEEPSEEK_API_KEY 尚未配置")
    model = DeepSeekRiskReviewModel(
        api_key=settings.deepseek_api_key.get_secret_value(),
        model_name=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
    )
    context = RiskReviewContext(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        employee_id="EMP-001",
        entitlement_id="insighthub.customer_export",
        duration_days=14,
        justification="核验虚构项目运营数据",
        entitlement_risk_level="high",
        approval_policy="manager_and_data_owner",
        policies=[
            PolicyMatch(
                policy_code="POL-003",
                title="高风险权限双审批",
                content="高风险权限必须依次经过直属经理和数据所有者审批。",
                similarity=0.95,
            )
        ],
    )
    print(model.review(context).model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "adapter",
        choices=("dashscope-embedding", "deepseek-risk", "all"),
    )
    args = parser.parse_args()
    settings = Settings()
    if args.adapter in {"dashscope-embedding", "all"}:
        smoke_dashscope_embedding(settings)
    if args.adapter in {"deepseek-risk", "all"}:
        smoke_deepseek_risk(settings)


if __name__ == "__main__":
    main()
