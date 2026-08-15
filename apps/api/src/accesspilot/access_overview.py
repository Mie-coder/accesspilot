"""权限业务卡片的确定性事实查询。

该模块只读取当前 Workspace 绑定员工的目录、正式申请和真实授权。
卡片状态不从聊天文本或模型输出推断，调用方也不能通过请求体覆盖身份。
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from accesspilot.db.models import (
    AccessGrantRecord,
    AccessRequestRecord,
    ApprovalCaseRecord,
    EmployeeRecord,
    EntitlementRecord,
    SystemRecord,
    WorkspaceRecord,
)
from accesspilot.db.workspace_store import hash_workspace_token

AccessOverviewState = Literal[
    "eligible",
    "owned",
    "pending",
    "expiring_soon",
    "expired",
]


class AccessOverviewItem(BaseModel):
    """前端权限卡片的稳定、无身份外泄字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AccessOverviewState
    code: str
    name: str
    system_code: str
    system_name: str
    risk_level: str
    max_duration_days: int | None
    approval_policy: str
    request_id: str | None
    request_status: str | None
    grant_id: str | None
    starts_at: datetime | None
    expires_at: datetime | None
    next_step: str


class AccessOverview(BaseModel):
    """当前员工的权限卡片事实集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AccessOverviewItem]


class AccessOverviewNotFoundError(LookupError):
    """Workspace 或其当前绑定员工不存在。"""


_STATE_ORDER: dict[AccessOverviewState, int] = {
    "owned": 0,
    "expiring_soon": 1,
    "pending": 2,
    "expired": 3,
    "eligible": 4,
}
_REJECTED_REQUEST_STATUSES = frozenset({"rejected", "denied", "cancelled", "canceled"})
_REJECTED_APPROVAL_STATUSES = frozenset({"rejected", "denied", "cancelled", "canceled"})
_SEVEN_DAYS = timedelta(days=7)


def _as_utc(value: datetime) -> datetime:
    """数据库驱动可能返回 naive 时间；统一按 UTC 比较，输出保留原始值。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_rejected(
    request: AccessRequestRecord,
    approval_status: str | None,
) -> bool:
    """只有明确拒绝/取消的正式申请才不算 pending。"""

    if request.request_status.casefold() in _REJECTED_REQUEST_STATUSES:
        return True
    return approval_status is not None and approval_status.casefold() in _REJECTED_APPROVAL_STATUSES


def _pending_next_step(
    approval_status: str | None,
    request_status: str,
    approval_policy: str,
) -> str:
    """根据已持久化审批事实生成下一步提示。"""

    normalized = approval_status.casefold() if approval_status is not None else ""
    if "data_owner" in normalized:
        return "等待数据所有者审批"
    if "manager" in normalized:
        return "等待直属经理审批"
    if normalized == "approved" or request_status.casefold() == "approved":
        return "审批已通过，等待权限开通"
    if approval_policy == "manager_and_data_owner":
        return "等待直属经理审批"
    if approval_policy == "manual_security":
        return "等待人工安全流程处理"
    if approval_policy == "manager":
        return "等待直属经理审批"
    return "等待审批流程启动"


def _item(
    *,
    state: AccessOverviewState,
    entitlement: EntitlementRecord,
    system: SystemRecord,
    request: AccessRequestRecord | None = None,
    grant: AccessGrantRecord | None = None,
    next_step: str,
) -> AccessOverviewItem:
    return AccessOverviewItem(
        state=state,
        code=entitlement.code,
        name=entitlement.name,
        system_code=system.code,
        system_name=system.name,
        risk_level=entitlement.risk_level,
        max_duration_days=entitlement.max_duration_days,
        approval_policy=entitlement.approval_policy,
        request_id=str(request.id) if request is not None else None,
        request_status=request.request_status if request is not None else None,
        grant_id=str(grant.id) if grant is not None else None,
        starts_at=grant.starts_at if grant is not None else None,
        expires_at=grant.expires_at if grant is not None else None,
        next_step=next_step,
    )


def _latest_by_code(
    records: list[AccessRequestRecord],
) -> dict[str, AccessRequestRecord]:
    """按正式申请创建时间选择每个权限的最新申请。"""

    latest: dict[str, AccessRequestRecord] = {}
    for request in records:
        previous = latest.get(request.entitlement_code)
        if previous is None or (
            (request.created_at, str(request.id))
            > (previous.created_at, str(previous.id))
        ):
            latest[request.entitlement_code] = request
    return latest


def _select_grant(
    grants: list[AccessGrantRecord],
    *,
    now: datetime,
) -> tuple[AccessGrantRecord | None, AccessOverviewState | None]:
    """在同一权限存在多条历史授权时选择当前最有意义的一条。"""

    if not grants:
        return None, None
    active = [
        grant
        for grant in grants
        if _as_utc(grant.starts_at) <= now < _as_utc(grant.expires_at)
    ]
    if active:
        grant = max(active, key=lambda item: (_as_utc(item.expires_at), str(item.id)))
        state: AccessOverviewState = (
            "expiring_soon"
            if _as_utc(grant.expires_at) <= now + _SEVEN_DAYS
            else "owned"
        )
        return grant, state

    expired = [grant for grant in grants if _as_utc(grant.expires_at) <= now]
    if expired:
        grant = max(expired, key=lambda item: (_as_utc(item.expires_at), str(item.id)))
        return grant, "expired"

    # 未来才生效的授权不是当前已拥有权限；保留其事实并让卡片继续等待。
    grant = min(grants, key=lambda item: (_as_utc(item.starts_at), str(item.id)))
    return grant, "pending"


def get_access_overview(
    session: Session,
    *,
    workspace_token: str,
    at: datetime | None = None,
) -> AccessOverview:
    """返回当前 Workspace/员工隔离后的权限生命周期卡片。"""

    if not workspace_token:
        raise AccessOverviewNotFoundError("Workspace 不存在")
    workspace = session.scalar(
        select(WorkspaceRecord).where(
            WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
        )
    )
    if workspace is None:
        raise AccessOverviewNotFoundError("Workspace 不存在")
    employee = session.get(EmployeeRecord, workspace.actor_id)
    if employee is None:
        raise AccessOverviewNotFoundError("当前员工不存在")

    now = _as_utc(at or datetime.now(UTC))
    eligible_rows = session.execute(
        select(EntitlementRecord, SystemRecord)
        .join(SystemRecord, SystemRecord.code == EntitlementRecord.system_code)
        .where(EntitlementRecord.self_service_allowed.is_(True))
        .order_by(EntitlementRecord.code)
    ).all()
    directory: dict[str, tuple[EntitlementRecord, SystemRecord]] = {
        entitlement.code: (entitlement, system)
        for entitlement, system in eligible_rows
        if employee.department in entitlement.eligible_departments
        or bool(set(employee.roles) & set(entitlement.eligible_roles))
    }

    requests = list(
        session.scalars(
            select(AccessRequestRecord)
            .where(
                AccessRequestRecord.workspace_id == workspace.id,
                AccessRequestRecord.requester_id == workspace.actor_id,
            )
            .order_by(AccessRequestRecord.created_at, AccessRequestRecord.id)
        ).all()
    )
    latest_requests = _latest_by_code(requests)
    request_by_id = {request.id: request for request in requests}
    approval_status_by_request: dict[object, str] = {}
    for case in session.scalars(
        select(ApprovalCaseRecord).where(
            ApprovalCaseRecord.workspace_id == workspace.id,
            ApprovalCaseRecord.request_id.in_(list(request_by_id)),
        )
    ).all():
        approval_status_by_request[case.request_id] = case.approval_status

    grants_by_code: dict[str, list[AccessGrantRecord]] = {}
    granted_request_ids: set[object] = set()
    if request_by_id:
        grants = session.scalars(
            select(AccessGrantRecord).where(
                AccessGrantRecord.workspace_id == workspace.id,
                AccessGrantRecord.request_id.in_(list(request_by_id)),
            )
        ).all()
        for candidate_grant in grants:
            request = request_by_id.get(candidate_grant.request_id)
            # 仅把同一 Workspace、当前员工的正式申请授权纳入卡片。
            if request is not None and request.requester_id == workspace.actor_id:
                granted_request_ids.add(candidate_grant.request_id)
                grants_by_code.setdefault(request.entitlement_code, []).append(
                    candidate_grant
                )

    all_codes = set(directory) | set(latest_requests) | set(grants_by_code)
    # 不能只看绝对最新申请：较新的取消/拒绝不应覆盖更早、仍在审批中的
    # 申请。pending 候选必须没有真实授权，且本身没有明确终止状态。
    pending_requests_by_code = _latest_by_code(
        [
            candidate
            for candidate in requests
            if candidate.id not in granted_request_ids
            and not _is_rejected(
                candidate,
                approval_status_by_request.get(candidate.id),
            )
        ]
    )
    items: list[AccessOverviewItem] = []
    for code in sorted(all_codes):
        directory_entry = directory.get(code)
        entitlement: EntitlementRecord
        system: SystemRecord
        if directory_entry is not None:
            entitlement, system = directory_entry
        else:
            row = session.execute(
                select(EntitlementRecord, SystemRecord)
                .join(SystemRecord, SystemRecord.code == EntitlementRecord.system_code)
                .where(EntitlementRecord.code == code)
            ).first()
            if row is None:
                # 旧申请引用已删除目录时不泄露不完整/不可展示的事实。
                continue
            entitlement, system = row

        request = pending_requests_by_code.get(code)
        approval_status = (
            approval_status_by_request.get(request.id) if request is not None else None
        )
        grant, grant_state = _select_grant(grants_by_code.get(code, []), now=now)
        grant_request = (
            request_by_id.get(grant.request_id) if grant is not None else None
        )
        has_pending_request = request is not None

        if grant_state in {"owned", "expiring_soon"} and grant is not None:
            items.append(
                _item(
                    state=grant_state,
                    entitlement=entitlement,
                    system=system,
                    request=grant_request,
                    grant=grant,
                    next_step=(
                        "当前可直接使用"
                        if grant_state == "owned"
                        else "即将到期，可发起续期申请"
                    ),
                )
            )
            if has_pending_request and request is not None:
                items.append(
                    _item(
                        state="pending",
                        entitlement=entitlement,
                        system=system,
                        request=request,
                        next_step=_pending_next_step(
                            approval_status,
                            request.request_status,
                            entitlement.approval_policy,
                        ),
                    )
                )
            continue

        if grant_state == "pending" and grant is not None:
            # starts_at 尚未到达：这是一条真实授权记录，但当前尚未生效。
            # 必须保留 grant/request 事实，不能退化为可重新申请的 eligible。
            items.append(
                _item(
                    state="pending",
                    entitlement=entitlement,
                    system=system,
                    request=grant_request,
                    grant=grant,
                    next_step="等待授权生效",
                )
            )
            if has_pending_request and request is not None and request.id != (
                grant_request.id if grant_request is not None else None
            ):
                items.append(
                    _item(
                        state="pending",
                        entitlement=entitlement,
                        system=system,
                        request=request,
                        next_step=_pending_next_step(
                            approval_status,
                            request.request_status,
                            entitlement.approval_policy,
                        ),
                    )
                )
            continue

        if grant_state == "expired" and grant is not None:
            expired_request = request_by_id.get(grant.request_id, request)
            items.append(
                _item(
                    state="expired",
                    entitlement=entitlement,
                    system=system,
                    request=expired_request,
                    grant=grant,
                    next_step="权限已过期，需要重新申请",
                )
            )
            if has_pending_request and request is not None:
                items.append(
                    _item(
                        state="pending",
                        entitlement=entitlement,
                        system=system,
                        request=request,
                        next_step=_pending_next_step(
                            approval_status,
                            request.request_status,
                            entitlement.approval_policy,
                        ),
                    )
                )
            # 过期授权不阻塞重新申请；只有没有正在审批的续期时，
            # 才额外返回一张 eligible 卡。
            elif code in directory:
                items.append(
                    _item(
                        state="eligible",
                        entitlement=entitlement,
                        system=system,
                        next_step="补充期限和业务理由后提交申请",
                    )
                )
            continue

        if has_pending_request and request is not None:
            items.append(
                _item(
                    state="pending",
                    entitlement=entitlement,
                    system=system,
                    request=request,
                    grant=None,
                    next_step=_pending_next_step(
                        approval_status,
                        request.request_status,
                        entitlement.approval_policy,
                    ),
                )
            )
            continue

        if code in directory:
            items.append(
                _item(
                    state="eligible",
                    entitlement=entitlement,
                    system=system,
                    next_step="补充期限和业务理由后提交申请",
                )
            )

    items.sort(key=lambda item: (item.code, _STATE_ORDER[item.state]))
    return AccessOverview(items=items)
