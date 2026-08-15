# AccessPilot 术语表

- Access Request：员工提交的访问权限申请。
- Request Draft：Agent 多轮对话中尚未确认的申请草稿。
- Approval Case：申请提交后产生的人工审批流程。
- Access Grant：审批通过并实际开通的权限记录。
- Entitlement：可申请的最小权限单位。
- Policy Clause：可被 RAG 检索和引用的政策条款。
- Agent Run：一次可暂停、恢复或重试的 Agent 执行过程。
- Provisioning：审批通过后实际执行权限开通的过程。
- Self-service：员工通过 Agent 发起申请，不代表可以绕过审批。
- Workspace Store：屏蔽存储细节的接口；当前由 SQLAlchemy 实现，将 Workspace 和 PostgreSQL 记录互相转换。
- Session：一次数据库会话和事务的工作单位；读取后关闭，写入后需要 commit 才能持久化。
