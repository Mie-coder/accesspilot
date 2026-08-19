"""T35 pending/checkpoint reconciliation, publish preflight and Legacy bridge.

Spec §10.2: before any Legacy cut-over the deployment must drain active turns
and then reconcile application pending projections against the accepted
checkpoint heads they reference.  This module owns:

- :class:`CheckpointTaskReader`: reads the retained interrupt task(s) at an
  exact ``checkpoint_thread_id + checkpoint_ns + accepted_checkpoint_id``
  without a compiled graph.  The v1.3 contract allows exactly zero or one
  ``__interrupt__`` write per accepted head; any other count fails closed;
- :class:`PublishPreflightGate`: the node-deletion/rename/reorder gate.  It
  passes only when live application pendings, unfinished executions and live
  accepted checkpoint tasks are all zero and every retained pending task is
  either part of a live run, exactly referenced by an immutable
  ``abandoned_*`` retirement tombstone, or provably superseded by a later
  accepted END head of the same run;
- :class:`LegacyRollbackBridge`: the executable rollback mapping per
  Workspace.  It runs under the same agent-thread advisory lock and lock order
  (Workspace row -> pending rows) as ``begin_input``/``begin_resume`` and
  re-reads every fact inside that boundary before writing, so it can never
  overwrite a concurrently accepted resume.  A reconciled confirmation whose
  Principal/Workspace/draft/revision all match maps to
  ``abandoned_to_legacy`` with a kept confirmation Cursor in one transaction
  (the Cursor is kept only when actor/session/kind fully match the pending,
  otherwise it is atomically rewritten); a reconciled head with
  principal/revision mismatch maps to ``abandoned_conflict`` and clears the
  Cursor; any unreadable/unknown/mismatched/orphan/unsuperseded state blocks
  with zero flow/Cursor/pending modification.  Dry run reports without
  writing and the bridge never deletes or downgrades checkpoint data or
  business facts.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accesspilot.agent.checkpoint import CheckpointLocator, SaverLike
from accesspilot.agent.turn_execution import TurnExecutionService
from accesspilot.db.models import (
    AgentPendingInputRecord,
    AgentTurnExecutionRecord,
    AuthSessionRecord,
    WorkspaceRecord,
    utc_now,
)
from accesspilot.db.workspace_store import hash_workspace_token
from accesspilot.workspaces import UnknownWorkspaceError

_INTERRUPT_CHANNEL = "__interrupt__"
_LIVE_PENDING_STATUSES = ("active", "resuming")
_ABANDONED_STATUSES = ("abandoned_to_legacy", "abandoned_conflict")
_GRAPH_SCHEMA_VERSION = 1
_GRAPH_FLOW_VERSION = 2

ROLLBACK_REASON_TO_LEGACY = (
    "rollback to legacy: drained, reconciled confirmation mapped to flow v1"
)
ROLLBACK_REASON_CONFLICT = (
    "rollback to legacy: reconciled head but principal or revision mismatch"
)
_RESOLVED_TASK_NOT_SUPERSEDED = (
    "retained interrupt task is neither live nor retired by an abandoned_* "
    "tombstone nor provably superseded by a later accepted END head"
)


class CheckpointTaskUnreadableError(RuntimeError):
    """The exact accepted head is missing or its task cannot be parsed."""


class RollbackBlockedError(RuntimeError):
    """The preflight/mapping gate blocked the operation with zero writes."""

    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True)
class RetainedTask:
    """Parsed interrupt task facts from one exact accepted checkpoint head."""

    checkpoint_id: str
    task_id: str
    kind: str
    pending_input_id: UUID
    draft_revision: int
    graph_run_id: UUID
    workspace_ref: UUID
    schema_version: int
    flow_version: int


def _as_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise CheckpointTaskUnreadableError(f"{field} is not a valid UUID")


def _pending_writes(tup: object) -> Sequence[Any]:
    """Typed access to the CheckpointTuple pending-write rows."""

    raw = getattr(tup, "pending_writes", None)
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise CheckpointTaskUnreadableError(
            "checkpoint pending writes are malformed"
        )
    return raw


def _interrupt_payloads(pending_writes: Sequence[Any]) -> list[Any]:
    """Collect every ``__interrupt__`` payload at the exact head.

    A malformed interrupt value (non-dict payload) is unreadable, not
    ignorable: the gate must classify all retained interrupts, never only the
    first one.
    """
    payloads: list[Any] = []
    for write in pending_writes:
        try:
            _task_id, channel, value = write
        except (TypeError, ValueError):
            raise CheckpointTaskUnreadableError(
                "pending write tuple is malformed"
            ) from None
        if channel != _INTERRUPT_CHANNEL:
            continue
        interrupts = value if isinstance(value, (tuple, list)) else (value,)
        for interrupt in interrupts:
            payload = getattr(interrupt, "value", None)
            if payload is None and isinstance(interrupt, dict):
                payload = interrupt.get("value")
            if payload is None or not isinstance(payload, dict):
                raise CheckpointTaskUnreadableError(
                    "interrupt payload is not a plain dict"
                )
            payloads.append(payload)
    return payloads


class CheckpointTaskReader:
    """Read the retained interrupt task at an exact accepted head.

    The read is always by the exact ``checkpoint_thread_id + checkpoint_ns +
    checkpoint_id`` recorded by the application projection, so a stale or
    guessed locator can never be answered by an implicit latest read.
    """

    def __init__(self, saver: SaverLike) -> None:
        self._saver = saver

    def read(self, locator: CheckpointLocator) -> RetainedTask | None:
        """Return the retained task, ``None`` when the head has no interrupt
        task, and raise :class:`CheckpointTaskUnreadableError` when the exact
        tuple is missing, the payload cannot be parsed, or the head carries
        more than one interrupt task (the v1.3 graph allows at most one)."""
        tup = self._saver.get_tuple(locator.as_config())
        if tup is None:
            raise CheckpointTaskUnreadableError(
                f"checkpoint head {locator.checkpoint_id} is unreadable"
            )
        checkpoint = getattr(tup, "checkpoint", None)
        channel_values = (
            checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
        )
        if not isinstance(channel_values, dict):
            raise CheckpointTaskUnreadableError(
                f"checkpoint head {locator.checkpoint_id} has no channel values"
            )
        payloads = _interrupt_payloads(_pending_writes(tup))
        if not payloads:
            return None
        if len(payloads) > 1:
            raise CheckpointTaskUnreadableError(
                f"checkpoint head {locator.checkpoint_id} retains "
                f"{len(payloads)} interrupt tasks; the v1.3 contract allows "
                "exactly one"
            )
        payload = payloads[0]
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise CheckpointTaskUnreadableError(
                f"interrupt payload at {locator.checkpoint_id} has no kind"
            )
        raw_pending_id = payload.get("pending_input_id")
        if not isinstance(raw_pending_id, str):
            raise CheckpointTaskUnreadableError(
                f"interrupt payload at {locator.checkpoint_id} has no pending id"
            )
        raw_revision = payload.get("draft_revision")
        if not isinstance(raw_revision, int):
            raise CheckpointTaskUnreadableError(
                f"interrupt payload at {locator.checkpoint_id} has no draft revision"
            )
        schema_version = channel_values.get("schema_version")
        flow_version = channel_values.get("flow_version")
        if not isinstance(schema_version, int) or not isinstance(flow_version, int):
            raise CheckpointTaskUnreadableError(
                f"checkpoint head {locator.checkpoint_id} has no graph version"
            )
        task_id = None
        for write in _pending_writes(tup):
            try:
                task_id, channel, _value = write
            except (TypeError, ValueError):
                continue
            if channel == _INTERRUPT_CHANNEL:
                break
        return RetainedTask(
            checkpoint_id=locator.checkpoint_id,
            task_id=task_id or "",
            kind=kind,
            pending_input_id=_as_uuid(raw_pending_id, "pending_input_id"),
            draft_revision=raw_revision,
            graph_run_id=_as_uuid(
                channel_values.get("graph_run_id"), "graph_run_id"
            ),
            workspace_ref=_as_uuid(
                channel_values.get("workspace_ref"), "workspace_ref"
            ),
            schema_version=schema_version,
            flow_version=flow_version,
        )


def classify_task(
    task: RetainedTask,
    pending: AgentPendingInputRecord,
    *,
    require_thread: bool = True,
) -> list[str]:
    """Two-way projection/task reconciliation; empty list means agreement.

    Compares pending ID, kind, run, checkpoint thread, root namespace and
    graph version exactly as Spec §10.2 requires.
    """
    reasons: list[str] = []
    if task.kind != "confirmation":
        reasons.append(f"unknown pending kind {task.kind!r}")
    if task.kind != pending.kind:
        reasons.append("task kind does not match the pending row kind")
    if task.pending_input_id != pending.pending_input_id:
        reasons.append("task pending_input_id does not match the pending row")
    if task.graph_run_id != pending.graph_run_id:
        reasons.append("task run does not match the pending row run")
    if require_thread:
        expected_thread = f"accesspilot:v1.3:{pending.graph_run_id}"
        if pending.checkpoint_thread_id != expected_thread:
            reasons.append("pending checkpoint thread does not match its run")
        if pending.checkpoint_ns != "":
            reasons.append("pending checkpoint namespace is not the root namespace")
    if task.workspace_ref != pending.workspace_id:
        reasons.append("task workspace does not match the pending row workspace")
    if (
        task.schema_version != _GRAPH_SCHEMA_VERSION
        or task.flow_version != _GRAPH_FLOW_VERSION
    ):
        reasons.append("task graph version is not the v1.3 flow-2 contract")
    return reasons


RetainedTaskState = Literal["live", "tombstoned", "superseded", "blocked"]


def pending_is_superseded(
    session: Session,
    reader: CheckpointTaskReader,
    pending: AgentPendingInputRecord,
) -> bool:
    """Prove that a resolved pending's interrupt was consumed.

    The interrupt at the pending's accepted head is superseded only when the
    same run has a later terminal execution at ``resume_input_seq`` whose
    accepted head advanced to a different checkpoint that retains no interrupt
    task.  Status alone (``resolved``) is never proof.
    """
    if pending.status != "resolved" or pending.resume_input_seq is None:
        return False
    continuation = session.scalar(
        select(AgentTurnExecutionRecord)
        .where(
            AgentTurnExecutionRecord.workspace_id == pending.workspace_id,
            AgentTurnExecutionRecord.graph_run_id == pending.graph_run_id,
            AgentTurnExecutionRecord.input_seq == pending.resume_input_seq,
            AgentTurnExecutionRecord.terminal_event_id.is_not(None),
            AgentTurnExecutionRecord.accepted_checkpoint_id.is_not(None),
            AgentTurnExecutionRecord.accepted_checkpoint_id
            != pending.accepted_checkpoint_id,
        )
        .order_by(
            AgentTurnExecutionRecord.created_at.desc(),
            AgentTurnExecutionRecord.input_seq.desc(),
        )
        .limit(1)
    )
    if continuation is None:
        return False
    head = continuation.accepted_checkpoint_id
    assert head is not None
    try:
        task = reader.read(
            CheckpointLocator(
                continuation.checkpoint_thread_id,
                continuation.checkpoint_ns,
                head,
            )
        )
    except CheckpointTaskUnreadableError:
        return False
    return task is None


def classify_retained_task(
    session: Session,
    reader: CheckpointTaskReader,
    task: RetainedTask,
    pending: AgentPendingInputRecord,
) -> tuple[list[str], RetainedTaskState]:
    """Classify one retained task against its pending row.

    Returns ``(reasons, state)``: empty reasons mean the task is accounted
    for; ``blocked`` carries the fail-closed reasons.  ``live`` tasks belong
    to an active/resuming pending, ``tombstoned`` tasks are exactly covered
    by the pending's immutable retirement tombstone, ``superseded`` tasks were
    consumed by a later accepted END head.
    """
    reasons = classify_task(task, pending)
    if reasons:
        return reasons, "blocked"
    if pending.status in _LIVE_PENDING_STATUSES:
        return [], "live"
    if pending.status in _ABANDONED_STATUSES:
        return [], "tombstoned"
    if pending_is_superseded(session, reader, pending):
        return [], "superseded"
    return (
        [
            f"pending {pending.pending_input_id}: {_RESOLVED_TASK_NOT_SUPERSEDED}"
        ],
        "blocked",
    )


@dataclass(frozen=True)
class PublishPreflightReport:
    """The publish/node-evolution gate verdict and its live zero-values."""

    passed: bool
    reasons: tuple[str, ...]
    live_pending_count: int
    unfinished_execution_count: int
    live_accepted_task_count: int
    retained_task_count: int
    superseded_task_count: int = 0


class PublishPreflightGate:
    """Node deletion/rename/reorder preflight: all live facts must be zero and
    every retained pending task must classify (live run, retirement tombstone
    or provable supersession)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        task_reader: CheckpointTaskReader,
    ) -> None:
        self._session_factory = session_factory
        self._task_reader = task_reader

    def check(
        self,
        *,
        workspace_ids: Collection[UUID] | None = None,
    ) -> PublishPreflightReport:
        """Check the whole deployment, or only the given Workspaces.

        The production gate checks everything (``workspace_ids=None``);
        scoping exists for drills and rollback of a bounded Workspace set.
        """
        with self._session_factory() as session:
            workspace_scope = (
                WorkspaceRecord.id.in_(workspace_ids)
                if workspace_ids is not None
                else WorkspaceRecord.id.is_not(None)
            )
            pendings = list(
                session.scalars(
                    select(AgentPendingInputRecord)
                    .join(
                        WorkspaceRecord,
                        AgentPendingInputRecord.workspace_id == WorkspaceRecord.id,
                    )
                    .where(workspace_scope)
                    .order_by(AgentPendingInputRecord.created_at)
                )
            )
            executions = list(
                session.scalars(
                    select(AgentTurnExecutionRecord)
                    .join(
                        WorkspaceRecord,
                        AgentTurnExecutionRecord.workspace_id == WorkspaceRecord.id,
                    )
                    .where(workspace_scope)
                    .order_by(AgentTurnExecutionRecord.created_at)
                )
            )
            reasons: list[str] = []
            live_pending = 0
            unfinished = 0
            live_tasks = 0
            retained_tasks = 0
            superseded_tasks = 0
            pending_by_id: dict[UUID, AgentPendingInputRecord] = {}
            for pending in pendings:
                pending_by_id[pending.pending_input_id] = pending
                task = self._read_pending_task(pending, reasons)
                if task is None:
                    if pending.status in _LIVE_PENDING_STATUSES:
                        live_pending += 1
                        reasons.append(
                            f"live pending {pending.pending_input_id} retains "
                            "no interrupt task at its accepted head"
                        )
                    continue
                task_reasons, state = classify_retained_task(
                    session, self._task_reader, task, pending
                )
                reasons.extend(task_reasons)
                if pending.status in _LIVE_PENDING_STATUSES:
                    live_pending += 1
                    live_tasks += 1
                elif state == "tombstoned":
                    retained_tasks += 1
                elif state == "superseded":
                    superseded_tasks += 1
            referenced_heads = {
                pending.accepted_checkpoint_id for pending in pendings
            }
            for execution in executions:
                if execution.terminal_event_id is None:
                    unfinished += 1
                head = execution.accepted_checkpoint_id
                if head is None or head in referenced_heads:
                    continue
                task = self._read_execution_task(execution, reasons)
                if task is None:
                    continue
                owning_pending = pending_by_id.get(task.pending_input_id)
                if owning_pending is None:
                    live_tasks += 1
                    reasons.append(
                        f"orphan accepted head {head} retains an interrupt task "
                        "that no pending row references"
                    )
                    continue
                task_reasons, state = classify_retained_task(
                    session, self._task_reader, task, owning_pending
                )
                reasons.extend(f"stale head {head}: {reason}" for reason in task_reasons)
                if state == "tombstoned":
                    retained_tasks += 1
                elif state == "superseded":
                    superseded_tasks += 1
                elif state == "live":
                    live_tasks += 1
            passed = (
                not reasons
                and live_pending == 0
                and unfinished == 0
                and live_tasks == 0
            )
            return PublishPreflightReport(
                passed=passed,
                reasons=tuple(reasons),
                live_pending_count=live_pending,
                unfinished_execution_count=unfinished,
                live_accepted_task_count=live_tasks,
                retained_task_count=retained_tasks,
                superseded_task_count=superseded_tasks,
            )

    def _read_pending_task(
        self,
        pending: AgentPendingInputRecord,
        reasons: list[str],
    ) -> RetainedTask | None:
        try:
            locator = self._locator_for(pending)
        except ValueError as error:
            reasons.append(f"pending {pending.pending_input_id}: {error}")
            return None
        try:
            return self._task_reader.read(locator)
        except CheckpointTaskUnreadableError as error:
            reasons.append(f"pending {pending.pending_input_id}: {error}")
            return None

    def _read_execution_task(
        self,
        execution: AgentTurnExecutionRecord,
        reasons: list[str],
    ) -> RetainedTask | None:
        try:
            locator = CheckpointLocator(
                execution.checkpoint_thread_id,
                execution.checkpoint_ns,
                execution.accepted_checkpoint_id or "",
            )
        except ValueError as error:
            reasons.append(f"execution {execution.input_turn_id}: {error}")
            return None
        try:
            return self._task_reader.read(locator)
        except CheckpointTaskUnreadableError as error:
            reasons.append(
                f"execution {execution.input_turn_id}: {error}"
            )
            return None

    @staticmethod
    def _locator_for(pending: AgentPendingInputRecord) -> CheckpointLocator:
        return CheckpointLocator(
            pending.checkpoint_thread_id,
            pending.checkpoint_ns,
            pending.accepted_checkpoint_id,
        )


@dataclass(frozen=True)
class PendingRollbackOutcome:
    """One pending row's executable Legacy mapping."""

    pending_input_id: UUID
    kind: str
    status_before: str
    mapping: Literal["abandoned_to_legacy", "abandoned_conflict"]
    retired_at: datetime | None
    retirement_reason: str | None


@dataclass(frozen=True)
class WorkspaceRollbackReport:
    """The per-Workspace rollback plan or result."""

    workspace_id: UUID
    dry_run: bool
    blocked: bool
    reasons: tuple[str, ...]
    flow_version_before: int
    flow_version_after: int | None
    cursor_action: Literal["none", "created", "kept", "cleared"] | None
    pending_outcomes: tuple[PendingRollbackOutcome, ...]
    conflict: bool


class LegacyRollbackBridge:
    """Spec §10.2 executable rollback mapping for one Workspace.

    Both ``plan`` and ``execute`` run under the Workspace's agent-thread
    advisory lock — the same boundary as ``begin_input``/``begin_resume``/
    ``takeover`` — and re-read every fact inside that boundary (Workspace row
    locked first, pending rows locked second, executions read-only to avoid
    the takeover ``execution -> workspace`` order) before any write.  A
    concurrently committed resume therefore either precedes the mapping (the
    bridge re-reads ``resuming``/running facts and blocks) or follows it (the
    resume fails on the tombstone), never both.

    ``plan`` is a pure dry run (reports only); ``execute`` applies the same
    plan atomically in one transaction.  Blocking paths raise
    :class:`RollbackBlockedError` from ``execute`` and are reported (never
    written) by ``plan``.  The bridge never deletes checkpoints, never touches
    execution/event/business rows and never reverse-syncs business facts.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        task_reader: CheckpointTaskReader,
    ) -> None:
        self._session_factory = session_factory
        self._task_reader = task_reader

    def plan(self, *, workspace_token: str) -> WorkspaceRollbackReport:
        """Dry run: read and report the mapping without writing anything."""
        return self._with_lock(workspace_token, dry_run=True)

    def execute(self, *, workspace_token: str) -> WorkspaceRollbackReport:
        """Execute the mapping atomically, or raise with zero writes."""
        return self._with_lock(workspace_token, dry_run=False)

    def _with_lock(
        self,
        workspace_token: str,
        *,
        dry_run: bool,
    ) -> WorkspaceRollbackReport:
        """Hold the agent-thread advisory lock across the whole mapping."""
        turn_service = TurnExecutionService(self._session_factory)
        agent_thread_id = self._agent_thread_id(workspace_token)
        with turn_service.advisory_lock(agent_thread_id) as lock:
            with lock.session.begin():
                return self._run(lock.session, workspace_token, dry_run=dry_run)

    def _agent_thread_id(self, workspace_token: str) -> UUID:
        with self._session_factory() as session:
            thread_id = session.scalar(
                select(WorkspaceRecord.agent_thread_id).where(
                    WorkspaceRecord.token_hash
                    == hash_workspace_token(workspace_token)
                )
            )
        if thread_id is None:
            raise UnknownWorkspaceError(workspace_token)
        return thread_id

    def _run(
        self,
        session: Session,
        workspace_token: str,
        *,
        dry_run: bool,
    ) -> WorkspaceRollbackReport:
        # Everything is re-read inside the advisory lock; the Workspace row is
        # locked first (matching begin_input/begin_resume), pending rows are
        # locked next, and execution rows are deliberately read-only so the
        # takeover execution -> workspace order can never deadlock here.
        workspace = session.scalar(
            select(WorkspaceRecord)
            .where(
                WorkspaceRecord.token_hash == hash_workspace_token(workspace_token)
            )
            .with_for_update()
        )
        if workspace is None:
            raise UnknownWorkspaceError(workspace_token)
        flow_version_before = workspace.flow_version
        reasons: list[str] = []
        executions = list(
            session.scalars(
                select(AgentTurnExecutionRecord)
                .where(AgentTurnExecutionRecord.workspace_id == workspace.id)
                .order_by(AgentTurnExecutionRecord.created_at)
            )
        )
        for execution in executions:
            if execution.terminal_event_id is None:
                reasons.append(
                    "workspace has unfinished executions; drain before rollback"
                )
                break
        pendings = list(
            session.scalars(
                select(AgentPendingInputRecord)
                .where(AgentPendingInputRecord.workspace_id == workspace.id)
                .order_by(AgentPendingInputRecord.created_at)
                .with_for_update()
            )
        )
        live = [
            pending
            for pending in pendings
            if pending.status in _LIVE_PENDING_STATUSES
        ]
        outcomes: list[PendingRollbackOutcome] = []
        cursor_action: Literal["none", "created", "kept", "cleared"] | None = None
        conflict = False

        # 1. Accepted execution heads that no pending row references must not
        # hide a retained task; classify every retained task against its
        # pending (orphans, unknown kinds and unsuperseded resolved tasks
        # block before any mapping).
        referenced_heads = {pending.accepted_checkpoint_id for pending in pendings}
        for execution in executions:
            head = execution.accepted_checkpoint_id
            if head is None or head in referenced_heads:
                continue
            try:
                task = self._task_reader.read(
                    CheckpointLocator(
                        execution.checkpoint_thread_id,
                        execution.checkpoint_ns,
                        head,
                    )
                )
            except (CheckpointTaskUnreadableError, ValueError) as error:
                reasons.append(f"execution {execution.input_turn_id}: {error}")
                continue
            if task is None:
                continue
            pending = next(
                (
                    candidate
                    for candidate in pendings
                    if candidate.pending_input_id == task.pending_input_id
                ),
                None,
            )
            if pending is None:
                reasons.append(
                    f"orphan accepted head {head} retains an interrupt task "
                    "that no pending row references"
                )
                continue
            task_reasons, _state = classify_retained_task(
                session, self._task_reader, task, pending
            )
            reasons.extend(
                f"stale head {head}: {reason}" for reason in task_reasons
            )

        # 2. Map every live pending (at most one per Workspace by DB
        # constraint) from its exact accepted head.  Writes are deferred until
        # the whole plan is clean so a blocked plan can never partially write.
        pending_writes: list[tuple[AgentPendingInputRecord, str, str]] = []
        for pending in live:
            try:
                locator = self._locator_for(pending)
                task = self._task_reader.read(locator)
            except (CheckpointTaskUnreadableError, ValueError) as error:
                reasons.append(f"pending {pending.pending_input_id}: {error}")
                continue
            if task is None:
                reasons.append(
                    f"live pending {pending.pending_input_id} retains no "
                    "interrupt task at its accepted head"
                )
                continue
            reasons.extend(
                f"pending {pending.pending_input_id}: {reason}"
                for reason in classify_task(task, pending)
            )
            if reasons:
                continue
            if self._clean_mapping(session, workspace, pending, task):
                mapping: Literal["abandoned_to_legacy", "abandoned_conflict"] = (
                    "abandoned_to_legacy"
                )
                reason = ROLLBACK_REASON_TO_LEGACY
                cursor_action = (
                    "kept"
                    if self._cursor_matches_pending(workspace, pending)
                    else "created"
                )
            else:
                mapping = "abandoned_conflict"
                reason = ROLLBACK_REASON_CONFLICT
                cursor_action = "cleared"
                conflict = True
            now = datetime.now(UTC)
            outcomes.append(
                PendingRollbackOutcome(
                    pending_input_id=pending.pending_input_id,
                    kind=pending.kind,
                    status_before=pending.status,
                    mapping=mapping,
                    retired_at=now,
                    retirement_reason=reason,
                )
            )
            pending_writes.append((pending, mapping, reason))

        # 2b. Non-live pendings must not hide retained tasks: a resolved
        # pending whose accepted head still retains an interrupt task blocks
        # unless provably superseded; abandoned rows are the retirement
        # tombstones themselves and need no re-verification here.
        for pending in pendings:
            if pending.status in _LIVE_PENDING_STATUSES:
                continue
            if pending.status in _ABANDONED_STATUSES:
                continue
            try:
                task = self._task_reader.read(self._locator_for(pending))
            except (CheckpointTaskUnreadableError, ValueError) as error:
                reasons.append(f"pending {pending.pending_input_id}: {error}")
                continue
            if task is None:
                continue
            task_reasons, _state = classify_retained_task(
                session, self._task_reader, task, pending
            )
            reasons.extend(task_reasons)

        blocked = bool(reasons)
        if blocked:
            if dry_run:
                return WorkspaceRollbackReport(
                    workspace_id=workspace.id,
                    dry_run=True,
                    blocked=True,
                    reasons=tuple(reasons),
                    flow_version_before=flow_version_before,
                    flow_version_after=None,
                    cursor_action=None,
                    pending_outcomes=tuple(outcomes),
                    conflict=conflict,
                )
            raise RollbackBlockedError(reasons)

        # 3. No blocking fact: the Workspace maps to flow 1 as a whole, with
        # the Cursor action and immutable tombstones applied in one transaction.
        if not dry_run:
            if not live:
                cursor_action = "none"
            workspace.flow_version = 1
            if cursor_action == "created":
                pending = live[0]
                workspace.cursor_actor_id = pending.actor_id
                workspace.cursor_auth_session_id = str(pending.auth_session_ref)
                workspace.cursor_expected_field = "confirmation"
                workspace.cursor_last_question_kind = "confirmation"
                workspace.cursor_issued_at = utc_now()
                workspace.cursor_consumed_at = None
            elif cursor_action == "cleared":
                workspace.cursor_actor_id = None
                workspace.cursor_auth_session_id = None
                workspace.cursor_expected_field = None
                workspace.cursor_last_question_kind = None
                workspace.cursor_issued_at = None
                workspace.cursor_consumed_at = None
            for pending, mapping_name, reason in pending_writes:
                pending.status = mapping_name
                pending.retired_at = utc_now()
                pending.retirement_reason = reason
                pending.updated_at = utc_now()
        elif not live:
            cursor_action = "none"
        return WorkspaceRollbackReport(
            workspace_id=workspace.id,
            dry_run=dry_run,
            blocked=False,
            reasons=(),
            flow_version_before=flow_version_before,
            flow_version_after=1,
            cursor_action=cursor_action,
            pending_outcomes=tuple(outcomes),
            conflict=conflict,
        )

    @staticmethod
    def _cursor_matches_pending(
        workspace: WorkspaceRecord,
        pending: AgentPendingInputRecord,
    ) -> bool:
        """A Cursor may be kept only when actor, auth session, expected field,
        question kind and active state all match the verified pending."""
        return (
            workspace.cursor_actor_id == pending.actor_id
            and workspace.cursor_auth_session_id == str(pending.auth_session_ref)
            and workspace.cursor_expected_field == "confirmation"
            and workspace.cursor_last_question_kind == "confirmation"
            and workspace.cursor_consumed_at is None
        )

    @staticmethod
    def _clean_mapping(
        session: Session,
        workspace: WorkspaceRecord,
        pending: AgentPendingInputRecord,
        task: RetainedTask,
    ) -> bool:
        """Principal/Workspace/draft/revision all-match predicate of §10.2."""
        workspace_match = task.workspace_ref == workspace.id
        revision_match = (
            task.draft_revision == pending.draft_revision
            and workspace.draft_revision == pending.draft_revision
            and workspace.draft is not None
        )
        auth = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.id == pending.auth_session_ref,
                AuthSessionRecord.workspace_id == workspace.id,
                AuthSessionRecord.employee_id == pending.actor_id,
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > datetime.now(UTC),
            )
        )
        principal_match = (
            pending.actor_id == workspace.actor_id and auth is not None
        )
        return workspace_match and revision_match and principal_match

    @staticmethod
    def _locator_for(pending: AgentPendingInputRecord) -> CheckpointLocator:
        return CheckpointLocator(
            pending.checkpoint_thread_id,
            pending.checkpoint_ns,
            pending.accepted_checkpoint_id,
        )
