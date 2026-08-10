"""将领域层 Workspace 保存到 PostgreSQL 的存储适配器。"""

from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import WorkspaceRecord
from accesspilot.domain.models import RequestDraft
from accesspilot.workspaces import Workspace


def hash_workspace_token(token: str) -> str:
    """将浏览器原始 Token 转为数据库可安全保存和查询的 SHA-256 哈希。"""

    return sha256(token.encode("utf-8")).hexdigest()


class SqlAlchemyWorkspaceStore:
    """使用 PostgreSQL 保存 Workspace，但不承担创建或重置等业务规则。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        # Session 工厂可以按需创建独立数据库会话。
        self._session_factory = session_factory

    def get(self, token: str) -> Workspace | None:
        """根据 Token 获取 Workspace，并将数据库记录还原为领域对象。"""

        token_hash = hash_workspace_token(token)

        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == token_hash
                )
            )

            if record is None:
                return None

            # JSONB 中的字典通过 Pydantic 恢复为有校验的草稿对象。
            draft = (
                RequestDraft.model_validate(record.draft)
                if record.draft is not None
                else None
            )

            # Session 稍后会关闭，因此返回独立的领域对象，而非 ORM 记录。
            return Workspace(
                token=token,
                actor_id=record.actor_id,
                demo_actor_id=record.demo_actor_id,
                demo_session_active=record.demo_session_active,
                draft=draft,
                fault_mode=record.fault_mode,
            )

    def save(self, workspace: Workspace) -> None:
        """创建或更新 Workspace 的可变状态，然后提交事务。"""

        token_hash = hash_workspace_token(workspace.token)
        draft_data = (
            workspace.draft.model_dump(mode="json")
            if workspace.draft is not None
            else None
        )

        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.token_hash == token_hash
                )
            )

            if record is None:
                # 第一次保存：创建一条新的数据库记录。
                session.add(
                    WorkspaceRecord(
                        token_hash=token_hash,
                        actor_id=workspace.actor_id,
                        demo_actor_id=workspace.demo_actor_id,
                        demo_session_active=workspace.demo_session_active,
                        draft=draft_data,
                        fault_mode=workspace.fault_mode,
                    )
                )
            else:
                # 后续保存：只更新允许变化的草稿和故障模式。
                record.draft = draft_data
                record.demo_actor_id = workspace.demo_actor_id
                record.demo_session_active = workspace.demo_session_active
                record.fault_mode = workspace.fault_mode
                record.actor_id = workspace.actor_id

            # commit 后，即使 API 进程重启，数据仍保留在 PostgreSQL 中。
            session.commit()
