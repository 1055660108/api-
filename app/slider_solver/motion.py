from __future__ import annotations

import math

from .types import Box


def build_drag_path(
    handle: Box,
    *,
    displacement: float,
    steps: int = 52,
    overshoot: float = 2.3,
) -> list[tuple[float, float]]:
    if steps < 8:
        raise ValueError("steps must be at least 8")
    if displacement <= 0:
        raise ValueError("displacement must be positive")
    if overshoot < 0:
        raise ValueError("overshoot must not be negative")

    start_x = handle.x + handle.width / 2
    start_y = handle.y + handle.height / 2
    points: list[tuple[float, float]] = []

    for index in range(steps + 1):
        progress = index / steps
        eased = 3 * progress**2 - 2 * progress**3
        x = start_x + (displacement + overshoot) * eased
        y = start_y + math.sin(index * 0.46) * 1.15
        points.append((x, y))

    target_x = start_x + displacement
    points.extend(
        [
            (target_x + overshoot * 0.85, start_y + 0.5),
            (target_x + overshoot * 0.48, start_y - 0.2),
            (target_x + overshoot * 0.15, start_y + 0.1),
            (target_x, start_y),
        ]
    )
    points[0] = (start_x, start_y)
    return points
