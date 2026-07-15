# Repository Guidelines

## Project Structure & Module Organization

AccessPilot is a workspace with a React demo UI and a Python API.

- `apps/api/src/accesspilot/` contains FastAPI code and domain logic. Keep business concepts in focused modules such as `domain/models.py` and `domain/workflow.py`.
- `apps/api/tests/` mirrors API source areas; for example, domain tests belong in `apps/api/tests/domain/`.
- `apps/web/src/` contains the Vite + React application. Place page-level UI in feature folders as the app grows; keep reusable UI and API clients separate.
- `docs/plans/` holds product plans. `docs/superpowers/plans/` holds the learner-facing delivery schedule.

All employees, systems, policies, and approval data must remain fictional. Never add Midea data, internal URLs, or credentials.

## Build, Test, and Development Commands

Use Python 3.11 or later and create a local virtual environment:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest apps/api/tests -v
ruff check apps/api/src apps/api/tests
mypy apps/api/src
```

For the web app, run `npm install` once, then use `npm run dev:web` for Vite, `npm run test:web` for Vitest, `npm run lint:web` for ESLint, and `npm run build:web` for the production build.

## Coding Style & Naming Conventions

Python uses four-space indentation, type annotations, Pydantic models, `snake_case` functions, and `PascalCase` classes. Ruff enforces imports and common correctness rules; MyPy runs in strict mode.

TypeScript uses two-space indentation, `PascalCase` React components, camelCase functions, and explicit interfaces for API payloads. Prefer domain names such as `AccessRequest`, `ApprovalCase`, and `AccessGrant` over vague names like `data` or `item`.

## Testing Guidelines

Use pytest for backend behavior and Vitest/Testing Library for UI interactions. Write one failing test before production behavior, verify the failure, then implement the smallest passing change. Name tests by observable behavior, e.g. `test_unconfirmed_request_cannot_be_submitted`. Cover state transitions, workspace isolation, and idempotent provisioning.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commit pattern: `feat: add request draft contract`, `fix: prevent duplicate grant`, or `docs: explain policy retrieval`. Keep commits small and runnable. Pull requests should state the user-visible behavior, tests run, relevant API or schema changes, and include screenshots for UI changes.

## Security & Agent Boundaries

Store keys only in `.env`; commit `.env.example` with placeholders. Do not expose model keys to React. Agents may explain, research, test, and review, but contributors must understand and be able to explain every merged change.

## 学习进度协议

每次开始辅导前，先读取 `docs/superpowers/plans/2026-07-12-accesspilot-learning-schedule.md` 和 `git status`，明确告诉学习者：当前 Day、已完成检查点、下一项检查点。只有获得测试输出、运行结果、代码或提交记录后，才能将对应的 `[ ]` 改为 `[x]`；不得根据推测勾选。默认采用教学模式：学习者亲手编写核心业务逻辑，Agent 主动补充测试、运行 pytest/Ruff/MyPy 等验证并处理测试与规范问题。除非学习者明确要求，不得直接实现业务功能。要求学习者编码前，必须先用中文说明业务问题、文件职责、数据流、关键语法和面试表达，并用一个理解问题确认学习者明白后再给编码步骤。

## 跨日复盘协议

每次开始新的 Day 前，辅导 Agent 必须先回顾上一天：说明完成的业务能力、涉及的文件和数据流、关键 Python/FastAPI 语法、重要安全或架构决策，以及已验证的测试结果。然后提出 3–5 个由浅入深的问题，覆盖“是什么、为什么、异常时怎么办、面试如何表达”。确认学习者能用自己的话说明核心概念后，才开始当天的新检查点。复盘问题和答案不得作为勾选检查点的唯一证据，但应作为后续讲解深度的依据。

当学习者表示“不理解”、答案不完整或明确要求提示时，必须采用苏格拉底式引导：一次只提出一个更小的具体场景或问题，帮助学习者从已知事实推导答案；不得立刻给出完整答案。可按“具体场景 → 第一层提示 → 第二层提示 → 完整解释与小例子”的顺序推进，并在每一层等待学习者回应。只有学习者仍无法推导时，才给出完整解释。首次出现的新 Python 语法、类型注解或框架机制时，也必须先用普通语言和小例子解释，再要求学习者编码。

苏格拉底式引导只用于业务设计、代码逻辑、架构取舍和面试理解。安装依赖、启动服务、环境配置、格式化和工具故障等机械操作由 Agent 直接执行并简要说明结果，不得用连续反问拖慢学习进度。
