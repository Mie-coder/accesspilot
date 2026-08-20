"""T37 test-only fault injector contract.

The crash controls are deliberately process-local and never travel through a
graph runtime context, API request, or UI payload.
"""

from __future__ import annotations

import pytest

from accesspilot.agent.fault_injection import (
    FaultPoint,
    InjectedCrash,
    hit_fault,
    inject_fault,
)


@pytest.mark.parametrize("point", list(FaultPoint))
def test_fault_points_are_default_off_and_scoped_single_shot(
    point: FaultPoint,
) -> None:
    hit_fault(point)

    with inject_fault(point):
        with pytest.raises(InjectedCrash, match=point.value):
            hit_fault(point)
        # One injection models one process death. A second hit in the same
        # scope stays disabled so cleanup/assertions cannot manufacture a
        # second crash.
        hit_fault(point)

    hit_fault(point)


def test_fault_injection_rejects_unknown_points() -> None:
    with pytest.raises(ValueError, match="unknown T37 fault point"):
        inject_fault("public-api-controlled-crash")
