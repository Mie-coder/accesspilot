# AccessPilot MVP Implementation Plan

## Global constraints

- All employees, systems, entitlements, policies, and approvals are fictional.
- Stack: React + TypeScript + Vite; FastAPI; LangChain/LangGraph; PostgreSQL + pgvector; DeepSeek Chat; Bailian `text-embedding-v4` at 512 dimensions.
- The golden path is a time-limited `InsightHub / customer data export` request.
- Applicant confirmation and sequential manager then data-owner approval are mandatory before provisioning.
- Approval and provisioning are separate states; provisioning is idempotent and fault-injectable.
- SSE exposes message/tool/state summaries, never hidden chain-of-thought.
- The public demo uses isolated workspaces, quotas, and an explicit replay fallback.
- Deployment targets the existing Tencent Cloud host via isolated Docker Compose and Nginx.

## Task 1: Foundation and domain model

Create the monorepo scaffold, Python and web toolchains, domain Pydantic models, status transitions, fictional seed catalog, `CONTEXT.md`, and the two ADRs. Add test-first coverage for validation, approval ordering, and state transitions.

## Task 2: Persistence and business APIs

Implement PostgreSQL-ready SQLAlchemy persistence with a SQLite test profile, demo-workspace isolation, request/approval/grant/audit repositories, idempotent IAM simulator, and FastAPI endpoints for workspaces, approvals, request details, role/fault switching, and reset. Add test-first API and integration coverage.

## Task 3: Agent runtime, RAG, and streaming

Implement model and embedding provider interfaces, DeepSeek and Bailian adapters, deterministic offline fallbacks, the bounded orchestrator and risk-reviewer LangGraph workflow, tool validation, confirmation guard, checkpoint-compatible state, SSE event storage/replay, quotas, and recoverable failures. Add test-first workflow, SSE, RAG, and safety-invariant coverage.

## Task 4: React demo application

Implement the three-page responsive UI: conversation workspace, approval inbox, and request/audit detail. Add the demo toolbar for actor, fault mode, reset, and replay indicators. Render streaming events, tool cards, draft confirmation, policy citations, and provisioning recovery. Add component and interaction tests.

## Task 5: Evaluation, deployment, and acceptance

Add the 12-scenario evaluation set and runner, Dockerfiles, Compose, Nginx SSE configuration, migrations/seed startup, health checks, resource limits, log rotation, environment examples, and a concise README. Run the full backend/frontend suites, type/lint/build checks, Docker validation, and final requirement audit.

