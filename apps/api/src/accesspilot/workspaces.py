"""Demo Workspace 的内存存储与业务操作。"""
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import Protocol
from uuid import UUID, uuid4

from accesspilot.domain.models import ConversationCursor, RequestDraft


class UnknownWorkspaceError(Exception):
    """请求的workspace token不存在"""


class InvalidDemoActorError(ValueError):
    """请求的员工不是前端允许切换的演示身份。"""


class DraftRevisionConflictError(RuntimeError):
    """草稿已被另一轮更新，拒绝覆盖较新的 revision。"""


class CursorConflictError(RuntimeError):
    """当前 Cursor 不再匹配调用方持有的 revision 或待补字段。"""


DEFAULT_DEMO_ACTOR_ID = "EMP-001"
EXPOSED_DEMO_ACTOR_IDS = frozenset({"EMP-001", "EMP-002", "EMP-003", "EMP-004"})


@dataclass
class Workspace:
    """一个访客独立拥有的演示空间。"""

    token: str
    workspace_id: UUID | str | None = None
    # 演示身份归后端 Workspace 所有，不从业务请求体信任员工编号。
    actor_id: str = DEFAULT_DEMO_ACTOR_ID
    demo_actor_id: str | None = None
    demo_session_active: bool = False
    draft: RequestDraft | None = None
    draft_revision: int = 0
    cursor: ConversationCursor | None = None
    # Bound by the authenticated request context.  It is not a client input.
    auth_session_id: str | None = None
    fault_mode: str | None = None

    def active_cursor(self) -> ConversationCursor | None:
        """返回仍绑定当前 Workspace/actor/revision 的活动游标。"""

        cursor = self.cursor
        if cursor is None or not cursor.is_active:
            return None
        if cursor.actor_id != self.actor_id:
            return None
        if self.auth_session_id is not None and cursor.auth_session_id != self.auth_session_id:
            return None
        if cursor.draft_revision != self.draft_revision:
            return None
        if (
            self.workspace_id is not None
            and str(cursor.workspace_id) != str(self.workspace_id)
        ):
            return None
        return cursor

    def clear_cursor(self, *, consumed: bool = False) -> None:
        """清除当前活动游标；``consumed`` 保留消费审计时间。"""

        if self.cursor is None:
            return
        if consumed:
            self.cursor = self.cursor.consume()
        else:
            self.cursor = None

    def effective_actor_id(self, product_actor_id: str, demo_mode_enabled: bool) -> str:
        """根据服务端配置解析当前业务请求使用的身份。"""

        if demo_mode_enabled and self.demo_session_active and self.demo_actor_id:
            return self.demo_actor_id
        return product_actor_id

    def effective_fault_mode(self, demo_mode_enabled: bool) -> str | None:
        """故障注入只在显式激活的 Demo 会话内生效。"""

        if demo_mode_enabled and self.demo_session_active:
            return self.fault_mode
        return None

class WorkspaceStore(Protocol):
    """Workspace 存储层必须遵守的接口。"""

    def get(self, token:str) -> Workspace | None : ...
    def save(self, workspace: Workspace) -> None : ...
    def update_draft_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        draft: RequestDraft,
        auth_session_id: str | None = None,
    ) -> bool: ...
    def consume_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        expected_field: str,
        draft: RequestDraft,
        auth_session_id: str,
    ) -> bool: ...

    def activate_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        cursor: ConversationCursor,
        auth_session_id: str,
    ) -> bool: ...

    def clear_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        auth_session_id: str,
    ) -> bool: ...

class InMemoryWorkspaceStore:
    """Day 3 使用的临时内存存储；服务重启后数据会消失。"""
    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
    
    def get(self, token: str) -> Workspace | None:
        return self._workspaces.get(token)
    
    def save(self, workspace: Workspace) -> None:
        self._workspaces[workspace.token] = workspace

    def update_draft_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        draft: RequestDraft,
        auth_session_id: str | None = None,
    ) -> bool:
        workspace = self._workspaces.get(token)
        if workspace is None or workspace.draft_revision != expected_revision:
            return False
        if workspace.cursor is not None and (
            auth_session_id is None
            or workspace.cursor.auth_session_id != auth_session_id
        ):
            return False
        workspace.draft = draft
        workspace.draft_revision += 1
        workspace.clear_cursor()
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
        workspace = self._workspaces.get(token)
        if workspace is None:
            return False
        cursor = workspace.active_cursor()
        if (
            workspace.draft_revision != expected_revision
            or cursor is None
            or cursor.expected_field != expected_field
            or cursor.auth_session_id != auth_session_id
        ):
            return False
        workspace.draft = draft
        workspace.draft_revision += 1
        workspace.cursor = cursor.consume()
        return True

    def activate_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        cursor: ConversationCursor,
        auth_session_id: str,
    ) -> bool:
        workspace = self._workspaces.get(token)
        if (
            workspace is None
            or workspace.draft_revision != expected_revision
            or cursor.actor_id != workspace.actor_id
            or cursor.auth_session_id != auth_session_id
        ):
            return False
        workspace.cursor = cursor
        return True

    def clear_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        auth_session_id: str,
    ) -> bool:
        workspace = self._workspaces.get(token)
        if workspace is None or workspace.draft_revision != expected_revision:
            return False
        workspace.clear_cursor()
        return True

class WorkspaceService:
    """封装创建、读取、保存草稿和重置的业务规则。"""
    def __init__(
        self,
        store: WorkspaceStore,
        *,
        product_actor_id: str = DEFAULT_DEMO_ACTOR_ID,
        demo_mode_enabled: bool = True,
    ) -> None:
        self._store = store
        self._product_actor_id = product_actor_id
        self._demo_mode_enabled = demo_mode_enabled

    def _normalize(self, workspace: Workspace) -> Workspace:
        """清理旧的或已失效的 Demo 覆盖，避免业务层读取过期身份。"""

        # Authenticated v1.2 workspaces are already bound to the Principal
        # selected by AuthSession.  Never rewrite EMP-002/003/004 back to the
        # legacy product actor while loading one of those sessions.
        if workspace.auth_session_id is not None:
            if workspace.cursor is not None and workspace.active_cursor() is None:
                workspace.clear_cursor()
            return workspace
        if not self._demo_mode_enabled:
            workspace.actor_id = self._product_actor_id
            workspace.demo_actor_id = None
            workspace.demo_session_active = False
            workspace.fault_mode = None
        elif not workspace.demo_session_active:
            workspace.actor_id = self._product_actor_id
            workspace.demo_actor_id = None
            workspace.fault_mode = None
        elif workspace.demo_actor_id:
            workspace.actor_id = workspace.demo_actor_id
        if workspace.cursor is not None and workspace.active_cursor() is None:
            # 身份切换、revision 变化或已消费游标都不能继续解释新输入。
            workspace.clear_cursor()
        return workspace

    def peek(self, token: str) -> Workspace:
        """读取原始 Workspace 状态，供 Cookie 边界迁移判断使用。"""

        workspace = self._store.get(token)
        if workspace is None:
            raise UnknownWorkspaceError(token)
        return workspace
    def create(self) -> Workspace:
        workspace = Workspace(
            token=token_urlsafe(32),
            workspace_id=uuid4(),
            actor_id=self._product_actor_id,
        )
        self._store.save(workspace)
        return workspace

    def get(self, token: str, *, auth_session_id: str | None = None) -> Workspace:
        workspace = self._store.get(token)
        if workspace is None:
            raise UnknownWorkspaceError(token)
        workspace.auth_session_id = auth_session_id
        before = (
            workspace.actor_id,
            workspace.demo_actor_id,
            workspace.demo_session_active,
            workspace.fault_mode,
            workspace.cursor,
        )
        normalized = self._normalize(workspace)
        after = (
            normalized.actor_id,
            normalized.demo_actor_id,
            normalized.demo_session_active,
            normalized.fault_mode,
            normalized.cursor,
        )
        if before != after:
            self._store.save(normalized)
        return normalized

    def _get_for_session(
        self, token: str, auth_session_id: str | None
    ) -> Workspace:
        """Preserve the legacy ``get(token)`` seam used by domain tests."""

        if auth_session_id is None:
            return self.get(token)
        return self.get(token, auth_session_id=auth_session_id)

    def save_draft(
        self,
        token: str,
        draft: RequestDraft,
        *,
        auth_session_id: str | None = None,
    ) -> Workspace:
        """Persist a draft while retaining the caller's AuthSession binding.

        The optional value keeps domain-only legacy fixtures usable, but every
        authenticated production route passes ``Workspace.auth_session_id``.
        That prevents ``_normalize`` from applying the old product actor to a
        private EMP-002/003/004 workspace and lets CAS enforce its cursor
        session binding.
        """

        workspace = self._get_for_session(token, auth_session_id)
        if workspace.draft == draft:
            return workspace
        if not self._store.update_draft_cas(
            token,
            expected_revision=workspace.draft_revision,
            draft=draft,
            auth_session_id=auth_session_id,
        ):
            raise DraftRevisionConflictError("草稿 revision 已发生变化")
        return self._get_for_session(token, auth_session_id)

    def save_draft_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        draft: RequestDraft,
        auth_session_id: str,
    ) -> Workspace:
        """按调用方持有的 revision 原子接受一次草稿更新。"""

        if not self._store.update_draft_cas(
            token,
            expected_revision=expected_revision,
            draft=draft,
            auth_session_id=auth_session_id,
        ):
            raise DraftRevisionConflictError("草稿 revision 已发生变化")
        return self._get_for_session(token, auth_session_id)

    def consume_cursor_cas(
        self,
        token: str,
        *,
        expected_revision: int,
        expected_field: str,
        draft: RequestDraft,
        auth_session_id: str,
    ) -> Workspace:
        """同时消费 Cursor、写入草稿并递增 revision。"""

        if not self._store.consume_cursor_cas(
            token,
            expected_revision=expected_revision,
            expected_field=expected_field,
            draft=draft,
            auth_session_id=auth_session_id,
        ):
            raise CursorConflictError("当前 Cursor 已失效或已被消费")
        return self._get_for_session(token, auth_session_id)

    def activate_cursor(
        self,
        token: str,
        *,
        expected_revision: int,
        expected_field: str,
        last_question_kind: str,
        auth_session_id: str,
    ) -> Workspace:
        """在追问事件成功持久化后激活绑定当前 revision 的 Cursor。"""

        if expected_field not in {
            "entitlement_id",
            "duration_days",
            "justification",
            "confirmation",
            "none",
        }:
            raise ValueError("不支持的 Cursor expected_field")
        workspace = self._get_for_session(token, auth_session_id)
        if workspace.draft_revision != expected_revision:
            raise DraftRevisionConflictError("追问绑定的草稿 revision 已变化")
        if expected_field == "none":
            if not self._store.clear_cursor_cas(
                token,
                expected_revision=expected_revision,
                auth_session_id=auth_session_id,
            ):
                raise DraftRevisionConflictError("追问绑定的草稿 revision 已变化")
            return self._get_for_session(token, auth_session_id)
        cursor = ConversationCursor(
            workspace_id=workspace.workspace_id or token,
            actor_id=workspace.actor_id,
            auth_session_id=auth_session_id,
            draft_revision=expected_revision,
            expected_field=expected_field,
            last_question_kind=last_question_kind,
            issued_at=datetime.now(UTC),
        )
        if not self._store.activate_cursor_cas(
            token,
            expected_revision=expected_revision,
            cursor=cursor,
            auth_session_id=auth_session_id,
        ):
            raise DraftRevisionConflictError("追问绑定的草稿 revision 已变化")
        return self._get_for_session(token, auth_session_id)

    def clear_cursor(
        self, token: str, *, auth_session_id: str
    ) -> Workspace:
        """显式帮助、重置或提交后使当前 Cursor 失效。"""

        workspace = self._get_for_session(token, auth_session_id)
        if workspace.cursor is not None:
            if not self._store.clear_cursor_cas(
                token,
                expected_revision=workspace.draft_revision,
                auth_session_id=auth_session_id,
            ):
                raise DraftRevisionConflictError("清除 Cursor 时草稿 revision 已变化")
            return self._get_for_session(token, auth_session_id)
        return workspace

    def set_actor(self, token: str, actor_id: str) -> Workspace:
        """切换后端演示身份，保留旧草稿以避免静默改写业务事实。"""

        if actor_id not in EXPOSED_DEMO_ACTOR_IDS:
            raise InvalidDemoActorError(actor_id)
        workspace = self.get(token)
        workspace.demo_actor_id = actor_id
        workspace.demo_session_active = True
        workspace.actor_id = actor_id
        workspace.clear_cursor()
        self._store.save(workspace)
        return workspace

    def reset(self, token: str) -> Workspace:
        workspace = self.get(token)
        workspace.draft = None
        workspace.draft_revision += 1
        workspace.clear_cursor()
        workspace.fault_mode = None
        self._store.save(workspace)
        return workspace

    def enter_demo(self, token: str, actor_id: str) -> Workspace:
        """显式进入虚构场景；身份覆盖只在该会话内有效。"""

        return self.set_actor(token, actor_id)


    def exit_demo(self, token: str) -> Workspace:
        """退出 Demo 并恢复产品身份，保留其他业务事实。"""

        workspace = self.get(token)
        workspace.actor_id = self._product_actor_id
        workspace.demo_actor_id = None
        workspace.demo_session_active = False
        workspace.fault_mode = None
        workspace.clear_cursor()
        self._store.save(workspace)
        return workspace

    def set_fault_mode(self, token: str, fault_mode: str | None) -> Workspace:
        """只修改当前演示空间的故障注入模式。"""

        workspace = self.get(token)
        workspace.fault_mode = fault_mode
        self._store.save(workspace)
        return workspace
