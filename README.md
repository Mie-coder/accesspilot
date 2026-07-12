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

- Day 1：申请草稿模型、缺失字段判断和测试已完成；
- Day 2：状态枚举、状态转换、虚构员工/权限/政策目录、术语表和 ADR 已完成；
- 当前正在搭建 FastAPI、数据库和 Agent 工具层。

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

当前阶段的核心测试不依赖 DeepSeek、百炼或 PostgreSQL，便于先验证领域规则。后续接入模型和向量服务时，密钥只能放在本地 `.env`，不得提交到 Git。

## 架构文档

- [访问申请与权限开通流程](docs/architecture/access-flow.md)
- [项目实现计划](docs/plans/accesspilot-mvp.md)
- [术语表](CONTEXT.md)
- [架构决策记录](docs/adr/)
