from __future__ import annotations

from decimal import Decimal, InvalidOperation


POINT_SCALE = 10
DEFAULT_MODEL_COST_UNITS = 10
MODEL_COST_UNITS = {
    "seedance 2.0": 10,
    "万相 2.7": 8,
    "万相 2.6": 5,
    "happyhorse 1.0": 8,
}


def points_to_units(value: object) -> int:
    try:
        points = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("积分格式无效")
    units = points * POINT_SCALE
    if points <= 0 or units != units.to_integral_value():
        raise ValueError("积分必须为正数且精确到0.1")
    return int(units)


def nonnegative_points_to_units(value: object) -> int:
    try:
        points = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("积分格式无效")
    units = points * POINT_SCALE
    if points < 0 or units != units.to_integral_value():
        raise ValueError("积分必须大于等于 0 且精确到 0.1")
    return int(units)


def units_to_points(units: int) -> int | float:
    value = max(0, int(units))
    return value // POINT_SCALE if value % POINT_SCALE == 0 else value / POINT_SCALE


def model_cost_units(platform: str, model: str, task_type: str = "video") -> int:
    from .config import load_settings

    settings = load_settings()
    platform_costs = settings.model_costs.get(str(platform or "").strip().lower(), {})
    for configured_model, points in platform_costs.items():
        if configured_model.casefold() == str(model or "").strip().casefold():
            return points_to_units(points)
    name = str(model or "").strip().casefold()
    return MODEL_COST_UNITS.get(name, DEFAULT_MODEL_COST_UNITS)


def model_cost_points(platform: str, model: str, task_type: str = "video") -> int | float:
    return units_to_points(model_cost_units(platform, model, task_type))


def package_bonus_free_uses(points: object) -> int:
    units = points_to_units(points)
    if units < 300:
        return 0
    return (units * 2 + 50) // 100
