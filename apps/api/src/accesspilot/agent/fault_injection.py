"""Process-local, test-only T37 crash injection boundaries.

Nothing in this module is wired to settings, dependency injection, HTTP, SSE,
or graph runtime context. Production therefore has no fault-control surface:
the context variable is empty unless an in-process test explicitly opens an
``inject_fault`` scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from enum import StrEnum


class FaultPoint(StrEnum):
    AFTER_QUOTA_COMMIT_BEFORE_CHECKPOINT = (
        "after_quota_commit_before_checkpoint"
    )
    AFTER_TOOL_COMPLETION_BEFORE_EVENT = "after_tool_completion_before_event"
    AFTER_DRAFT_CAS_BEFORE_CHECKPOINT = (
        "after_draft_cas_before_checkpoint"
    )
    AFTER_INTERRUPT_SAVED = "after_interrupt_saved"
    AFTER_NON_CONFIRM_RESUME_CONSUMED = "after_non_confirm_resume_consumed"
    AFTER_CONFIRMATION_CAS_BEFORE_TERMINAL = (
        "after_confirmation_cas_before_terminal"
    )


class InjectedCrash(RuntimeError):
    """A simulated process death propagated as a normal graph execution failure.

    The six calls live outside the graph's provider/tool recovery ``try``
    blocks, so the production graph never converts this test-only signal into
    a public recoverable outcome.  Using ``Exception`` semantics lets
    LangGraph reliably cancel/join node workers before the test models the
    process restart.
    """

    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        super().__init__(f"injected T37 crash: {point.value}")


_armed_faults: ContextVar[tuple[FaultPoint, ...]] = ContextVar(
    "accesspilot_t37_armed_faults",
    default=(),
)


def _validated_point(point: FaultPoint | str) -> FaultPoint:
    try:
        return FaultPoint(point)
    except ValueError:
        raise ValueError(f"unknown T37 fault point: {point}") from None


@contextmanager
def _injection_scope(point: FaultPoint) -> Iterator[None]:
    token = _armed_faults.set((*_armed_faults.get(), point))
    try:
        yield
    finally:
        _armed_faults.reset(token)


def inject_fault(point: FaultPoint | str) -> AbstractContextManager[None]:
    """Arm one single-shot crash point in the current in-process context."""

    return _injection_scope(_validated_point(point))


def hit_fault(point: FaultPoint) -> None:
    """Crash once when ``point`` is armed; otherwise remain a zero-cost no-op."""

    armed = _armed_faults.get()
    if point not in armed:
        return
    remaining = list(armed)
    remaining.remove(point)
    _armed_faults.set(tuple(remaining))
    raise InjectedCrash(point)
