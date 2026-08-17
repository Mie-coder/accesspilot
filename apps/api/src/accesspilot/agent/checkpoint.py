"""Official PostgresSaver lifecycle and exact, fenced checkpoint primitives."""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Iterator, Sequence
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult, make_url
from sqlalchemy.orm import Session

from accesspilot.agent.advisory_lock import AdvisoryLockHandle
from accesspilot.config import Settings
from accesspilot.db.models import AgentTurnExecutionRecord, WorkspaceRecord, utc_now


class CheckpointUnavailableError(RuntimeError):
    """The configured checkpoint store cannot serve runtime reads/writes."""


class ExactCheckpointRequired(RuntimeError):
    """A read attempted to omit or escape the accepted exact head."""


class CandidateCheckpointRejected(RuntimeError):
    """A saver candidate did not pass the post-stop exact-state gate."""


class SaverLike(Protocol):
    serde: Any

    def get_tuple(self, config: dict[str, Any]) -> object | None: ...

    def get_next_version(self, current: Any | None, channel: None) -> Any: ...

    def with_allowlist(
        self,
        extra_allowlist: Collection[tuple[str, ...]],
    ) -> SaverLike: ...

    def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]: ...

    def put_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None: ...


class CheckpointRuntimeLike(Protocol):
    def start(self) -> None: ...

    def check_readiness(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CheckpointLocator:
    """A complete, root-graph checkpoint coordinate."""

    checkpoint_thread_id: str
    checkpoint_ns: str
    checkpoint_id: str

    def __post_init__(self) -> None:
        if not self.checkpoint_thread_id.startswith("accesspilot:v1.3:"):
            raise ValueError("checkpoint_thread_id must be server generated")
        if self.checkpoint_ns != "":
            raise ValueError("the v1.3 root graph requires checkpoint_ns='' ")
        if not self.checkpoint_id:
            raise ValueError("checkpoint_id must be non-empty")

    def as_config(self) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": self.checkpoint_thread_id,
                "checkpoint_ns": self.checkpoint_ns,
                "checkpoint_id": self.checkpoint_id,
            }
        }

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> CheckpointLocator:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ExactCheckpointRequired("checkpoint config lacks configurable")
        checkpoint_id = configurable.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ExactCheckpointRequired("exact checkpoint_id is required")
        thread_id = configurable.get("thread_id")
        checkpoint_ns = configurable.get("checkpoint_ns")
        if not isinstance(thread_id, str) or not isinstance(checkpoint_ns, str):
            raise ExactCheckpointRequired("exact thread_id and checkpoint_ns are required")
        return cls(thread_id, checkpoint_ns, checkpoint_id)


_SERVER_CONTEXT_TOKEN = object()


@dataclass(frozen=True, init=False)
class ServerExecutionContext:
    """Immutable ownership facts copied only from a server-side execution row."""

    execution_id: UUID
    workspace_id: UUID
    graph_run_id: UUID
    input_seq: int
    input_turn_id: str
    checkpoint_thread_id: str
    checkpoint_ns: str
    lease_fence: int
    accepted_checkpoint_id: str | None

    def __init__(
        self,
        *,
        execution_id: UUID,
        workspace_id: UUID,
        graph_run_id: UUID,
        input_seq: int,
        input_turn_id: str,
        checkpoint_thread_id: str,
        checkpoint_ns: str,
        lease_fence: int,
        accepted_checkpoint_id: str | None,
        _token: object,
    ) -> None:
        if _token is not _SERVER_CONTEXT_TOKEN:
            raise TypeError("execution context must come from a server execution row")
        object.__setattr__(self, "execution_id", execution_id)
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "graph_run_id", graph_run_id)
        object.__setattr__(self, "input_seq", input_seq)
        object.__setattr__(self, "input_turn_id", input_turn_id)
        object.__setattr__(self, "checkpoint_thread_id", checkpoint_thread_id)
        object.__setattr__(self, "checkpoint_ns", checkpoint_ns)
        object.__setattr__(self, "lease_fence", lease_fence)
        object.__setattr__(self, "accepted_checkpoint_id", accepted_checkpoint_id)

    @classmethod
    def from_record(cls, record: AgentTurnExecutionRecord) -> ServerExecutionContext:
        if not isinstance(record, AgentTurnExecutionRecord):
            raise TypeError("execution context requires a server-side execution record")
        if record.status != "running" or record.lease_fence < 1:
            raise ValueError("execution context requires a running fenced owner")
        expected_thread = f"accesspilot:v1.3:{record.graph_run_id}"
        if record.checkpoint_thread_id != expected_thread or record.checkpoint_ns != "":
            raise ValueError("execution record has an invalid root checkpoint coordinate")
        return cls(
            execution_id=record.id,
            workspace_id=record.workspace_id,
            graph_run_id=record.graph_run_id,
            input_seq=record.input_seq,
            input_turn_id=record.input_turn_id,
            checkpoint_thread_id=record.checkpoint_thread_id,
            checkpoint_ns=record.checkpoint_ns,
            lease_fence=record.lease_fence,
            accepted_checkpoint_id=record.accepted_checkpoint_id,
            _token=_SERVER_CONTEXT_TOKEN,
        )

    @property
    def accepted_locator(self) -> CheckpointLocator | None:
        if self.accepted_checkpoint_id is None:
            return None
        return CheckpointLocator(
            self.checkpoint_thread_id,
            self.checkpoint_ns,
            self.accepted_checkpoint_id,
        )


_CANDIDATE_TOKEN = object()
_VERIFIED_TOKEN = object()


@dataclass(frozen=True, init=False)
class CandidateCheckpoint:
    context: ServerExecutionContext
    locator: CheckpointLocator
    parent_checkpoint_id: str | None

    def __init__(
        self,
        context: ServerExecutionContext,
        locator: CheckpointLocator,
        parent_checkpoint_id: str | None,
        *,
        _token: object,
    ) -> None:
        if _token is not _CANDIDATE_TOKEN:
            raise TypeError("candidates are created only by a saver invocation")
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "parent_checkpoint_id", parent_checkpoint_id)


@dataclass(frozen=True, init=False)
class VerifiedCheckpointCandidate:
    context: ServerExecutionContext
    locator: CheckpointLocator

    def __init__(
        self,
        context: ServerExecutionContext,
        locator: CheckpointLocator,
        *,
        _token: object,
    ) -> None:
        if _token is not _VERIFIED_TOKEN:
            raise TypeError("verified candidates require exact state validation")
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "locator", locator)


StateT = TypeVar("StateT")


@dataclass
class _InvocationState:
    candidate: CandidateCheckpoint | None = None


class FencedSaverInvocation:
    """Invocation-scoped saver facade that never performs an implicit-latest read."""

    def __init__(self, saver: SaverLike, context: ServerExecutionContext) -> None:
        self._saver = saver
        self.context = context
        self._invocation_state = _InvocationState()
        self._owned_candidate_ids: set[str] = set()
        self._owned_write_ids: set[str] = set()
        self.serde = getattr(saver, "serde", None)

    @property
    def candidate(self) -> CandidateCheckpoint | None:
        # LangGraph strict-msgpack applies a shallow serde clone.  The holder is
        # deliberately shared so the caller still observes the invocation-local
        # candidate after graph shutdown.
        return self._invocation_state.candidate

    @candidate.setter
    def candidate(self, value: CandidateCheckpoint | None) -> None:
        self._invocation_state.candidate = value

    def _validate_thread(self, config: dict[str, Any]) -> dict[str, Any]:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ExactCheckpointRequired("checkpoint config lacks configurable")
        if configurable.get("thread_id") != self.context.checkpoint_thread_id:
            raise ExactCheckpointRequired("checkpoint thread does not match execution")
        if configurable.get("checkpoint_ns") != self.context.checkpoint_ns:
            raise ExactCheckpointRequired("checkpoint namespace does not match execution")
        return configurable

    def get_tuple(self, config: dict[str, Any]) -> object | None:
        configurable = self._validate_thread(config)
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is None and self.context.accepted_checkpoint_id is None:
            if self._owned_candidate_ids:
                raise ExactCheckpointRequired("exact checkpoint_id is required after put")
            # A brand-new execution is known to have no accepted head.  Return
            # empty without querying official latest, which may be an orphan.
            return None
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ExactCheckpointRequired("exact checkpoint_id is required")
        allowed = checkpoint_id == self.context.accepted_checkpoint_id or (
            checkpoint_id in self._owned_candidate_ids
        )
        if not allowed:
            raise ExactCheckpointRequired("checkpoint_id is not the accepted head")
        return self._saver.get_tuple(CheckpointLocator.from_config(config).as_config())

    def get_exact(self, locator: CheckpointLocator) -> object:
        checkpoint = self.get_tuple(locator.as_config())
        if checkpoint is None:
            raise ExactCheckpointRequired("exact checkpoint head does not exist")
        return checkpoint

    def get_next_version(self, current: Any | None, channel: None) -> Any:
        """Preserve the official saver channel-version and concurrency semantics."""

        return self._saver.get_next_version(current, channel)

    def with_allowlist(
        self,
        extra_allowlist: Collection[tuple[str, ...]],
    ) -> FencedSaverInvocation:
        """Propagate strict serde types to the saver that performs the actual I/O."""

        delegated = self._saver.with_allowlist(extra_allowlist)
        if delegated is self._saver and getattr(delegated, "serde", None) is self.serde:
            return self
        clone = copy(self)
        clone._saver = delegated
        clone.serde = getattr(delegated, "serde", None)
        # copy() intentionally keeps the invocation holder and candidate-id sets
        # shared between the caller-facing facade and LangGraph's serde clone.
        return clone

    def list(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[object]:
        del config, filter, before, limit
        raise ExactCheckpointRequired("checkpoint history/list reads are forbidden")

    def delete_thread(self, thread_id: str) -> None:
        del thread_id
        raise ExactCheckpointRequired("checkpoint deletion is not a runtime operation")

    def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        configurable = self._validate_thread(config)
        parent_checkpoint_id = configurable.get("checkpoint_id")
        if parent_checkpoint_id is not None and not isinstance(
            parent_checkpoint_id, str
        ):
            raise ExactCheckpointRequired("parent checkpoint_id must be a string")
        if not self._owned_candidate_ids:
            if parent_checkpoint_id != self.context.accepted_checkpoint_id:
                raise ExactCheckpointRequired(
                    "first put parent must equal the accepted checkpoint head"
                )
        elif (
            self.candidate is None
            or parent_checkpoint_id != self.candidate.locator.checkpoint_id
        ):
            raise ExactCheckpointRequired(
                "subsequent put parent must be the invocation's immediate candidate"
            )
        checkpoint_id = checkpoint.get("id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ExactCheckpointRequired("checkpoint write requires checkpoint.id")
        if checkpoint_id == parent_checkpoint_id:
            raise ExactCheckpointRequired("checkpoint candidate must advance its parent")
        saved = self._saver.put(config, checkpoint, metadata, new_versions)
        locator = CheckpointLocator.from_config(saved)
        if (
            locator.checkpoint_thread_id != self.context.checkpoint_thread_id
            or locator.checkpoint_ns != self.context.checkpoint_ns
            or locator.checkpoint_id != checkpoint_id
        ):
            raise CandidateCheckpointRejected("official saver returned a mismatched locator")
        self._owned_candidate_ids.add(locator.checkpoint_id)
        self.candidate = CandidateCheckpoint(
            self.context,
            locator,
            parent_checkpoint_id,
            _token=_CANDIDATE_TOKEN,
        )
        return saved

    def put_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        locator = CheckpointLocator.from_config(config)
        self._validate_thread(config)
        if locator.checkpoint_id not in self._owned_candidate_ids and (
            locator.checkpoint_id != self.context.accepted_checkpoint_id
        ):
            # LangGraph records the brand-new input task against a complete,
            # generated checkpoint coordinate before its first checkpoint put.
            # It remains invocation-local and cannot become accepted by itself.
            if self.context.accepted_checkpoint_id is None and not self._owned_candidate_ids:
                self._owned_write_ids.add(locator.checkpoint_id)
            else:
                raise ExactCheckpointRequired(
                    "writes require an accepted or invocation candidate head"
                )
        self._saver.put_writes(config, writes, task_id, task_path)

    def verify_candidate(
        self,
        candidate: CandidateCheckpoint,
        *,
        graph_stopped: bool,
        graph_state_reader: Callable[[dict[str, Any]], StateT],
        state_validator: Callable[[StateT], bool],
    ) -> VerifiedCheckpointCandidate:
        if candidate is not self.candidate or candidate.context != self.context:
            raise CandidateCheckpointRejected("candidate is not invocation-local")
        if candidate.parent_checkpoint_id not in self._owned_candidate_ids and (
            candidate.parent_checkpoint_id != self.context.accepted_checkpoint_id
        ):
            raise CandidateCheckpointRejected("candidate lineage escaped the accepted head")
        if not graph_stopped:
            raise CandidateCheckpointRejected("graph must be stopped before promotion")
        if self.get_exact(candidate.locator) is None:  # pragma: no cover - get_exact raises
            raise CandidateCheckpointRejected("candidate exact tuple is missing")
        state = graph_state_reader(candidate.locator.as_config())
        state_config = getattr(state, "config", None)
        if not isinstance(state_config, dict):
            raise CandidateCheckpointRejected("graph state lacks an exact locator")
        try:
            state_locator = CheckpointLocator.from_config(state_config)
        except (ExactCheckpointRequired, ValueError) as error:
            raise CandidateCheckpointRejected("graph state locator is invalid") from error
        if state_locator != candidate.locator or not state_validator(state):
            raise CandidateCheckpointRejected("exact graph state validation failed")
        return VerifiedCheckpointCandidate(
            self.context,
            candidate.locator,
            _token=_VERIFIED_TOKEN,
        )


class FencedPostgresSaverAdapter:
    """Creates one isolated candidate scope for a server-owned execution."""

    def __init__(self, saver: SaverLike) -> None:
        self._saver = saver

    def for_execution(self, context: ServerExecutionContext) -> FencedSaverInvocation:
        if not isinstance(context, ServerExecutionContext):
            raise TypeError("fenced saver requires server execution context")
        invocation_type = _official_invocation_type()
        return invocation_type(self._saver, context)


@lru_cache(maxsize=1)
def _official_invocation_type() -> type[FencedSaverInvocation]:
    """Lazily add LangGraph's nominal base after strict-msgpack process setup."""

    from langgraph.checkpoint.base import BaseCheckpointSaver

    return type(
        "OfficialFencedSaverInvocation",
        (FencedSaverInvocation, BaseCheckpointSaver),
        {},
    )


class AcceptedCheckpointHeadStore:
    """CAS a verified candidate inside the caller-owned application transaction."""

    def promote(
        self,
        session: Session,
        verified: VerifiedCheckpointCandidate,
        *,
        lock: AdvisoryLockHandle | None = None,
    ) -> bool:
        if not isinstance(verified, VerifiedCheckpointCandidate):
            raise TypeError("head promotion requires a verified checkpoint candidate")
        if lock is not None:
            lock.require_session(session)
        context = verified.context
        now = datetime.now(UTC)
        statement = update(AgentTurnExecutionRecord).where(
            AgentTurnExecutionRecord.id == context.execution_id,
            AgentTurnExecutionRecord.workspace_id == context.workspace_id,
            AgentTurnExecutionRecord.workspace_id.in_(
                select(WorkspaceRecord.id).where(
                    WorkspaceRecord.id == context.workspace_id,
                    WorkspaceRecord.lease_fence == context.lease_fence,
                )
            ),
            AgentTurnExecutionRecord.graph_run_id == context.graph_run_id,
            AgentTurnExecutionRecord.input_seq == context.input_seq,
            AgentTurnExecutionRecord.input_turn_id == context.input_turn_id,
            AgentTurnExecutionRecord.status == "running",
            AgentTurnExecutionRecord.lease_fence == context.lease_fence,
            AgentTurnExecutionRecord.lease_expires_at.is_not(None),
            AgentTurnExecutionRecord.lease_expires_at > now,
            AgentTurnExecutionRecord.checkpoint_thread_id
            == context.checkpoint_thread_id,
            AgentTurnExecutionRecord.checkpoint_ns == context.checkpoint_ns,
        )
        if context.accepted_checkpoint_id is None:
            statement = statement.where(
                AgentTurnExecutionRecord.accepted_checkpoint_id.is_(None)
            )
        else:
            statement = statement.where(
                AgentTurnExecutionRecord.accepted_checkpoint_id
                == context.accepted_checkpoint_id
            )
        result = cast(
            CursorResult[Any],
            session.execute(
                statement.values(
                    accepted_checkpoint_id=verified.locator.checkpoint_id,
                    updated_at=utc_now(),
                )
            ),
        )
        promoted = result.rowcount == 1
        if lock is not None:
            # Close the transaction even on a stale/no-op CAS so the next
            # fenced operation on the same lock session can begin cleanly.
            session.commit()
        return promoted


def checkpoint_connection_kwargs(schema: str) -> dict[str, object]:
    if schema == "public" or not schema or not schema.replace("_", "a").isalnum():
        raise ValueError("invalid checkpoint schema")
    return {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "options": f"-c search_path={schema},public",
    }


def psycopg_connection_url(database_url: str) -> str:
    """Convert the configured SQLAlchemy URL without decoding credentials."""

    parsed = make_url(database_url)
    if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("checkpointing requires a PostgreSQL psycopg URL")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _assert_strict_msgpack_active() -> None:
    if os.getenv("LANGGRAPH_STRICT_MSGPACK", "").lower() not in {"1", "true", "yes"}:
        raise CheckpointUnavailableError(
            "LANGGRAPH_STRICT_MSGPACK must be enabled before checkpoint import"
        )
    from langgraph.checkpoint.serde import _msgpack

    if not _msgpack.STRICT_MSGPACK_ENABLED:
        raise CheckpointUnavailableError("strict msgpack was enabled after LangGraph import")


class PostgresCheckpointRuntime:
    """One synchronous pool and official saver per FastAPI application instance."""

    def __init__(self, settings: Settings) -> None:
        if not settings.checkpoint_database_url:
            raise CheckpointUnavailableError("checkpoint runtime URL is missing")
        self._settings = settings
        self.pool: ConnectionPool[Any] | None = None
        self.saver: SaverLike | None = None

    def start(self) -> None:
        if self.pool is not None:
            return
        _assert_strict_msgpack_active()
        from langgraph.checkpoint.postgres import PostgresSaver

        checkpoint_database_url = self._settings.checkpoint_database_url
        if checkpoint_database_url is None:  # guarded by __init__, retained for typing
            raise CheckpointUnavailableError("checkpoint runtime URL is missing")
        pool = ConnectionPool(
            psycopg_connection_url(checkpoint_database_url),
            min_size=self._settings.checkpoint_pool_min_size,
            max_size=self._settings.checkpoint_pool_max_size,
            kwargs=checkpoint_connection_kwargs(self._settings.checkpoint_schema),
            open=False,
        )
        pool.open(wait=False)
        self.pool = pool
        self.saver = cast(SaverLike, PostgresSaver(cast(Any, pool)))

    def check_readiness(self) -> None:
        if self.pool is None or self.saver is None:
            raise CheckpointUnavailableError("checkpoint runtime is not started")
        try:
            self.pool.wait(timeout=self._settings.checkpoint_readiness_timeout_seconds)
            missing = CheckpointLocator(
                "accesspilot:v1.3:readiness",
                "",
                "00000000-0000-0000-0000-000000000000",
            )
            # This is deliberately an exact miss: it exercises SELECT access to
            # all runtime tables without creating data or selecting latest.
            self.saver.get_tuple(missing.as_config())
        except Exception as error:
            raise CheckpointUnavailableError("checkpoint runtime is unavailable") from error

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()


def build_checkpoint_runtime(settings: Settings) -> PostgresCheckpointRuntime:
    return PostgresCheckpointRuntime(settings)
