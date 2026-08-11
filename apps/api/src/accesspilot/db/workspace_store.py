"""将领域层 Workspace 保存到 PostgreSQL 的存储适配器。"""

from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.db.models import WorkspaceRecord
from accesspilot.domain.models import ConversationCursor, RequestDraft
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
                workspace_id=record.id,
                actor_id=record.actor_id,
                demo_actor_id=record.demo_actor_id,
                demo_session_active=record.demo_session_active,
                draft=draft,
                draft_revision=record.draft_revision,
                cursor=(
                    ConversationCursor(
                        workspace_id=record.id,
                        actor_id=record.cursor_actor_id or record.actor_id,
                        auth_session_id=record.cursor_auth_session_id,
                        draft_revision=record.draft_revision,
                        expected_field=record.cursor_expected_field,
                        last_question_kind=(
                            record.cursor_last_question_kind
                            or record.cursor_expected_field
                            or "none"
                        ),
                        issued_at=record.cursor_issued_at or record.created_at,
                        consumed_at=record.cursor_consumed_at,
                    )
                    if record.cursor_expected_field is not None
                    and record.cursor_auth_session_id is not None
                    else None
                ),
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
                        draft_revision=workspace.draft_revision,
                        cursor_actor_id=(
                            workspace.cursor.actor_id
                            if workspace.cursor is not None
                            else None
                        ),
                        cursor_auth_session_id=(
                            workspace.cursor.auth_session_id
                            if workspace.cursor is not None
                            else None
                        ),
                        cursor_expected_field=(
                            workspace.cursor.expected_field
                            if workspace.cursor is not None
                            else None
                        ),
                        cursor_last_question_kind=(
                            workspace.cursor.last_question_kind
                            if workspace.cursor is not None
                            else None
                        ),
                        cursor_issued_at=(
                            workspace.cursor.issued_at
                            if workspace.cursor is not None
                            else None
                        ),
                        cursor_consumed_at=(
                            workspace.cursor.consumed_at
                            if workspace.cursor is not None
                            else None
                        ),
                        fault_mode=workspace.fault_mode,
                    )
                )
            else:
                # 后续保存：只更新允许变化的草稿和故障模式。
                record.draft = draft_data
                record.draft_revision = workspace.draft_revision
                record.cursor_actor_id = (
                    workspace.cursor.actor_id if workspace.cursor is not None else None
                )
                record.cursor_auth_session_id = (
                    workspace.cursor.auth_session_id if workspace.cursor is not None else None
                )
                record.cursor_expected_field = (
                    workspace.cursor.expected_field if workspace.cursor is not None else None
                )
                record.cursor_last_question_kind = (
                    workspace.cursor.last_question_kind
                    if workspace.cursor is not None
                    else None
                )
                record.cursor_issued_at = (
                    workspace.cursor.issued_at if workspace.cursor is not None else None
                )
                record.cursor_consumed_at = (
                    workspace.cursor.consumed_at if workspace.cursor is not None else None
                )
                record.demo_actor_id = workspace.demo_actor_id
                record.demo_session_active = workspace.demo_session_active
                record.fault_mode = workspace.fault_mode
                record.actor_id = workspace.actor_id

            # commit 后，即使 API 进程重启，数据仍保留在 PostgreSQL 中。
            session.commit()

    def update_draft_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        draft: RequestDraft,
        auth_session_id: str | None = None,
    ) -> bool:
        """在 Workspace 行锁内接受一次 draft + revision CAS 更新。"""

        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord)
                .where(WorkspaceRecord.token_hash == hash_workspace_token(token))
                .with_for_update()
            )
            if record is None or record.draft_revision != expected_revision:
                session.rollback()
                return False
            if record.cursor_expected_field is not None and (
                auth_session_id is None
                or record.cursor_auth_session_id != auth_session_id
            ):
                session.rollback()
                return False
            record.draft = draft.model_dump(mode="json")
            record.draft_revision += 1
            # 业务字段变化后旧 Cursor 绑定的 revision 已失效。
            record.cursor_actor_id = None
            record.cursor_auth_session_id = None
            record.cursor_expected_field = None
            record.cursor_last_question_kind = None
            record.cursor_issued_at = None
            record.cursor_consumed_at = None
            session.commit()
            return True

    def consume_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        expected_field: str,
        draft: RequestDraft,
        auth_session_id: str,
    ) -> bool:
        """在同一事务内写入期限、递增 revision 并消费活动 Cursor。"""

        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord)
                .where(WorkspaceRecord.token_hash == hash_workspace_token(token))
                .with_for_update()
            )
            if record is None:
                session.rollback()
                return False
            if (
                record.draft_revision != expected_revision
                or record.cursor_expected_field != expected_field
                or record.cursor_consumed_at is not None
                or record.cursor_actor_id != record.actor_id
                or record.cursor_auth_session_id != auth_session_id
            ):
                session.rollback()
                return False
            record.draft = draft.model_dump(mode="json")
            record.draft_revision += 1
            record.cursor_consumed_at = datetime.now(UTC)
            session.commit()
            return True

    def activate_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        cursor: ConversationCursor,
        auth_session_id: str,
    ) -> bool:
        """仅在 revision 未变化时原子写入下一追问 Cursor。"""

        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord)
                .where(WorkspaceRecord.token_hash == hash_workspace_token(token))
                .with_for_update()
            )
            if (
                record is None
                or record.draft_revision != expected_revision
                or cursor.actor_id != record.actor_id
                or (
                    record.cursor_expected_field is not None
                    and record.cursor_auth_session_id != auth_session_id
                )
            ):
                session.rollback()
                return False
            record.cursor_actor_id = cursor.actor_id
            record.cursor_auth_session_id = cursor.auth_session_id
            record.cursor_expected_field = cursor.expected_field
            record.cursor_last_question_kind = cursor.last_question_kind
            record.cursor_issued_at = cursor.issued_at
            record.cursor_consumed_at = cursor.consumed_at
            session.commit()
            return True

    def clear_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        auth_session_id: str,
    ) -> bool:
        """仅在 revision 未变化时原子清空 Cursor 列，避免整行覆盖草稿。"""

        with self._session_factory() as session:
            record = session.scalar(
                select(WorkspaceRecord)
                .where(WorkspaceRecord.token_hash == hash_workspace_token(token))
                .with_for_update()
            )
            if record is None or record.draft_revision != expected_revision:
                session.rollback()
                return False
            if record.cursor_expected_field is not None and (
                auth_session_id is None
                or record.cursor_auth_session_id != auth_session_id
            ):
                session.rollback()
                return False
            record.cursor_actor_id = None
            record.cursor_auth_session_id = None
            record.cursor_expected_field = None
            record.cursor_last_question_kind = None
            record.cursor_issued_at = None
            record.cursor_consumed_at = None
            session.commit()
            return True
