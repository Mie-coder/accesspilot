# AccessPilot

AccessPilot 是一个完全使用虚构数据的企业系统访问申请 Agent。它演示员工如何通过多轮对话提交权限申请，并经过风险审查、人工审批和实际权限开通。

## 项目目标

- 多轮对话补全结构化申请草稿；
- 使用 Structured Output 和 Tool Calling；
- 通过状态机管理确认、审批、开通和错误恢复；
- 使用主 Agent 与只读风险审查 Agent 协作；
- 使用 RAG 检索政策条款并返回稳定的政策编号；
- 区分“审批通过”和“权限已经开通”；
- 记录审计事件，并通过幂等键安全重试。

所有员工、系统、权限、审批和政策内容都是原创虚构数据，与任何真实企业内部数据无关。

## 当前进度

- T01：DeepSeek 严格结构化提取、确认提交守卫、正式申请与审计事务已完成；
- T02：百炼/离线 512 维向量、pgvector Top 4 和只读风险审查已完成；
- T03：直属经理到数据所有者的串行审批、越权/乱序/重复守卫与审计已完成；
- T04：幂等 IAM 开通、失败重试、超时未知查询恢复和唯一授权已完成；
- 下一步按 `docs/tickets/accesspilot-mvp-v1.md` 实现 SSE 事件回放与 Workspace 配额。

## 目录结构

```text
apps/api/src/accesspilot/    Python API 与领域代码
apps/api/tests/              后端 pytest 测试
apps/web/                    React + Vite 前端
docs/                        产品计划、架构流程和 ADR
CONTEXT.md                   项目术语表
```

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

pytest apps/api/tests -v
ruff check apps/api/src apps/api/tests
mypy apps/api/src
```

集成测试需要本地 PostgreSQL/pgvector；DeepSeek 与百炼测试默认使用假客户端或确定性离线适配器，不消耗真实额度。真实密钥只能放在本地 `.env`，不得提交到 Git：

```dotenv
DEEPSEEK_API_KEY=你的本地密钥
DASHSCOPE_API_KEY=你的本地密钥
```

需要手动验证真实适配器时运行：

```bash
python -m accesspilot.smoke dashscope-embedding
python -m accesspilot.smoke deepseek-risk
```

Smoke 输出只包含安全业务结果或向量维度，不打印密钥和完整向量。

## 架构文档

- [访问申请与权限开通流程](docs/architecture/access-flow.md)
- [数据库 ER 图](docs/architecture/data-model-er.md)
- [项目实现计划](docs/plans/accesspilot-mvp.md)
- [术语表](CONTEXT.md)
- [架构决策记录](docs/adr/)
