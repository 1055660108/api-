from __future__ import annotations

import cv2
import numpy as np

from .types import GapSolveResult


def _decode(encoded: bytes, mode: int, label: str) -> np.ndarray:
    array = np.frombuffer(encoded, dtype=np.uint8)
    image = cv2.imdecode(array, mode)
    if image is None:
        raise ValueError(f"could not decode {label} image")
    return image


def _gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    horizontal = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    vertical = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(horizontal, vertical)


def solve_gap(background_bytes: bytes, piece_bytes: bytes) -> GapSolveResult:
    """Locate the horizontal puzzle translation in source-image coordinates."""
    background = _decode(background_bytes, cv2.IMREAD_COLOR, "background")
    piece = _decode(piece_bytes, cv2.IMREAD_UNCHANGED, "piece")
    if piece.ndim != 3 or piece.shape[2] != 4:
        raise ValueError("piece image must contain an alpha channel")

    piece_height, piece_width = piece.shape[:2]
    image_height, image_width = background.shape[:2]
    if piece_width > image_width or piece_height > image_height:
        raise ValueError("piece image is larger than background image")

    alpha_mask = np.where(piece[:, :, 3] > 32, 255, 0).astype(np.uint8)
    if cv2.countNonZero(alpha_mask) == 0:
        raise ValueError("piece alpha mask is empty")

    background_gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    piece_gray = cv2.cvtColor(piece[:, :, :3], cv2.COLOR_BGR2GRAY)
    scores = cv2.matchTemplate(
        _gradient_magnitude(background_gray),
        _gradient_magnitude(piece_gray),
        cv2.TM_CCORR_NORMED,
        mask=alpha_mask,
    )
    _, confidence, _, best = cv2.minMaxLoc(scores)

    return GapSolveResult(
        target_x=float(best[0] + 3.0),
        confidence=float(confidence),
        image_width=image_width,
        image_height=image_height,
    )
