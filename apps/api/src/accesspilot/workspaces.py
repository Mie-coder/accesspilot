"""Demo Workspace 的内存存储与业务操作。"""
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Protocol

from accesspilot.domain.models import RequestDraft


class UnknownWorkspaceError(Exception):
    """请求的workspace token不存在"""

@dataclass
class Workspace:
    """一个访客独立拥有的演示空间。"""
    
    token: str
    draft: RequestDraft | None = None
    fault_mode: str | None = None

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
    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store
    
    def create(self) -> Workspace:
        workspace = Workspace(token=token_urlsafe(32))
        self._store.save(workspace)
        return workspace

    def get(self, token: str) -> Workspace:
        workspace = self._store.get(token)
        if workspace is None:
            raise UnknownWorkspaceError(token)
        return workspace

    def save_draft(self, token: str, draft: RequestDraft) -> Workspace:
        workspace = self.get(token)
        workspace.draft = draft
        self._store.save(workspace)
        return workspace

    def reset(self, token: str) -> Workspace:
        workspace = self.get(token)
        workspace.draft = None
        workspace.fault_mode = None
        self._store.save(workspace)
        return workspace

    def set_fault_mode(self, token: str, fault_mode: str | None) -> Workspace:
        """只修改当前演示空间的故障注入模式。"""

        workspace = self.get(token)
        workspace.fault_mode = fault_mode
        self._store.save(workspace)
        return workspace
