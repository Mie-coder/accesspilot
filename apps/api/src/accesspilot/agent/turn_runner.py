"""T33 fenced graph turn runner: advisory lock + heartbeat during invoke."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from accesspilot.agent.advisory_lock import AdvisoryLockHandle
from accesspilot.agent.turn_execution import (
    TurnExecutionHandle,
    TurnExecutionService,
)


class GraphInvoker(Protocol):
    """Minimal production graph invoke surface used by the runner."""

    def invoke(
        self,
        graph_input: Any,
        config: Any | None = None,
        *,
        context: Any,
        **kwargs: Any,
    ) -> Any: ...


class FencedGraphTurnRunner:
    """Run one graph turn while holding the thread lock and renewing the lease.

    The caller must already hold the ``AdvisoryLockHandle`` from
    ``TurnExecutionService.advisory_lock``.  The lock therefore continuously
    covers any preceding takeover, this graph invocation, and any subsequent
    head promotion/terminal finalize performed on the same lock session.
    """

    def __init__(
        self,
        graph: GraphInvoker,
        turn_service: TurnExecutionService,
        *,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._graph = graph
        self._turn_service = turn_service
        self._heartbeat_interval = heartbeat_interval

    def run(
        self,
        handle: TurnExecutionHandle,
        graph_input: Any,
        context: Any,
        lock: AdvisoryLockHandle,
        config: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke the graph under an already-held thread advisory lock.

        Before the graph is called, the lease is immediately CAS-renewed so an
        expired/stale handle fails with zero graph invocations.  A daemon
        heartbeat thread then keeps renewing while the synchronous graph call
        runs.
        """
        # Pre-flight: this must fail before graph.invoke for expired/stale
        # handles, leaving graph call count at zero.
        self._turn_service.heartbeat(handle)

        stop = threading.Event()
        heartbeat_errors: list[BaseException] = []

        def heartbeat_loop() -> None:
            try:
                while not stop.wait(self._heartbeat_interval):
                    self._turn_service.heartbeat(handle)
            except BaseException as error:  # pragma: no cover - failure path
                heartbeat_errors.append(error)

        thread = threading.Thread(target=heartbeat_loop, daemon=True)
        thread.start()
        try:
            return self._graph.invoke(
                graph_input,
                config,
                context=context,
                **kwargs,
            )
        finally:
            stop.set()
            thread.join(timeout=min(max(self._heartbeat_interval * 2, 1.0), 5.0))
            if heartbeat_errors:
                raise heartbeat_errors[0]
