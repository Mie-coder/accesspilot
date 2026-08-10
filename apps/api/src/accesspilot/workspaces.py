"""Demo Workspace 的内存存储与业务操作。"""
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Protocol

from accesspilot.domain.models import RequestDraft


class UnknownWorkspaceError(Exception):
    """请求的workspace token不存在"""


class InvalidDemoActorError(ValueError):
    """请求的员工不是前端允许切换的演示身份。"""


DEFAULT_DEMO_ACTOR_ID = "EMP-001"
EXPOSED_DEMO_ACTOR_IDS = frozenset({"EMP-001", "EMP-002", "EMP-003", "EMP-004"})


@dataclass
class Workspace:
    """一个访客独立拥有的演示空间。"""

    token: str
    # 演示身份归后端 Workspace 所有，不从业务请求体信任员工编号。
    actor_id: str = DEFAULT_DEMO_ACTOR_ID
    demo_actor_id: str | None = None
    demo_session_active: bool = False
    draft: RequestDraft | None = None
    fault_mode: str | None = None

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

class InMemoryWorkspaceStore:
    """Day 3 使用的临时内存存储；服务重启后数据会消失。"""
    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
    
    def get(self, token: str) -> Workspace | None:
        return self._workspaces.get(token)
    
    def save(self, workspace: Workspace) -> None:
        self._workspaces[workspace.token] = workspace

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
        return workspace

    def peek(self, token: str) -> Workspace:
        """读取原始 Workspace 状态，供 Cookie 边界迁移判断使用。"""

        workspace = self._store.get(token)
        if workspace is None:
            raise UnknownWorkspaceError(token)
        return workspace
    def create(self) -> Workspace:
        workspace = Workspace(token=token_urlsafe(32), actor_id=self._product_actor_id)
        self._store.save(workspace)
        return workspace

    def get(self, token: str) -> Workspace:
        workspace = self._store.get(token)
        if workspace is None:
            raise UnknownWorkspaceError(token)
        before = (
            workspace.actor_id,
            workspace.demo_actor_id,
            workspace.demo_session_active,
            workspace.fault_mode,
        )
        normalized = self._normalize(workspace)
        after = (
            normalized.actor_id,
            normalized.demo_actor_id,
            normalized.demo_session_active,
            normalized.fault_mode,
        )
        if before != after:
            self._store.save(normalized)
        return normalized

    def save_draft(self, token: str, draft: RequestDraft) -> Workspace:
        workspace = self.get(token)
        workspace.draft = draft
        self._store.save(workspace)
        return workspace

    def set_actor(self, token: str, actor_id: str) -> Workspace:
        """切换后端演示身份，保留旧草稿以避免静默改写业务事实。"""

        if actor_id not in EXPOSED_DEMO_ACTOR_IDS:
            raise InvalidDemoActorError(actor_id)
        workspace = self.get(token)
        workspace.demo_actor_id = actor_id
        workspace.demo_session_active = True
        workspace.actor_id = actor_id
        self._store.save(workspace)
        return workspace

    def reset(self, token: str) -> Workspace:
        workspace = self.get(token)
        workspace.draft = None
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
        self._store.save(workspace)
        return workspace

    def set_fault_mode(self, token: str, fault_mode: str | None) -> Workspace:
        """只修改当前演示空间的故障注入模式。"""

        workspace = self.get(token)
        workspace.fault_mode = fault_mode
        self._store.save(workspace)
        return workspace
