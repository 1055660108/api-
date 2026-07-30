from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .page_solver import SliderChallengeSolver
from .types import SliderSolveResult

T = TypeVar("T")


class SliderVerificationError(RuntimeError):
    """Raised when a visible slider cannot be resolved within the configured limit."""


async def resolve_slider_if_present(page: Any, solver: SliderChallengeSolver) -> SliderSolveResult:
    result = await solver.solve(page)
    if result.status in {"not_present", "success"}:
        return result
    raise SliderVerificationError(
        result.error or f"slider verification failed after {result.attempts} attempts"
    )


async def run_with_slider_recovery(
    page: Any,
    operation: Callable[[], Awaitable[T]],
    solver: SliderChallengeSolver,
) -> T:
    await resolve_slider_if_present(page, solver)
    try:
        value = await operation()
    except Exception as original_error:
        result = await resolve_slider_if_present(page, solver)
        if result.status != "success":
            raise original_error
        value = await operation()
    await resolve_slider_if_present(page, solver)
    return value
