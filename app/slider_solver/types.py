from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_mapping(cls, value: dict[str, float]) -> "Box":
        return cls(
            x=float(value["x"]),
            y=float(value["y"]),
            width=float(value["width"]),
            height=float(value["height"]),
        )


@dataclass(frozen=True, slots=True)
class GapSolveResult:
    target_x: float
    confidence: float
    image_width: int
    image_height: int

    def display_displacement(self, displayed_width: float) -> float:
        if displayed_width <= 0:
            raise ValueError("displayed_width must be positive")
        return self.target_x * displayed_width / self.image_width


@dataclass(frozen=True, slots=True)
class SliderSolverSettings:
    iframe_selector: str = "iframe[src*='bdcaptcha.html']"
    background_selector: str = "img[alt='basicImg']"
    piece_selector: str = "img[alt='actionImg']"
    handle_selector: str = ".captcha-slider-btn"
    refresh_text: str = "Refresh"
    verify_url_fragment: str = "/captcha/verify"
    max_attempts: int = 3
    refresh_before_solve: bool = True
    refresh_timeout_seconds: float = 5.0
    verify_timeout_seconds: float = 5.0
    drag_steps: int = 52
    drag_overshoot: float = 2.3
    step_delay_seconds: float = 0.012
    minimum_confidence: float = 0.45

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 8:
            raise ValueError("max_attempts must be between 1 and 8")
        if self.drag_steps < 8:
            raise ValueError("drag_steps must be at least 8")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class SliderSolveResult:
    status: str
    attempts: int
    displacement: float | None = None
    confidence: float | None = None
    verify_response: dict[str, Any] | None = field(default=None)
    error: str | None = None
