from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .store import get_meta, task_dir, task_image_paths, update_meta


LOGGER = logging.getLogger(__name__)
DETECTION_MAX_SIDE = 1600
MANIFEST_VERSION = 2
GRID_ALPHA = 0.4


def _source_fingerprint(path: Path) -> str:
    stat = path.stat()
    value = f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:20]


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": MANIFEST_VERSION, "images": {}}
    try:
        version = int(value.get("version") or 0) if isinstance(value, dict) else 0
    except (TypeError, ValueError):
        version = 0
    if not isinstance(value, dict) or version != MANIFEST_VERSION:
        return {"version": MANIFEST_VERSION, "images": {}}
    return value


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _load_image(path: Path) -> np.ndarray | None:
    try:
        encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def _overlap_ratio(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    if not intersection:
        return 0.0
    return intersection / float(min(aw * ah, bw * bh) or 1)


def _merge_faces(faces: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for face in sorted(faces, key=lambda item: item[2] * item[3], reverse=True):
        if any(_overlap_ratio(face, current) >= 0.55 for current in merged):
            continue
        merged.append(face)
    return merged


def _detect_faces(image: np.ndarray, *, retry: bool = False) -> list[tuple[int, int, int, int]]:
    height, width = image.shape[:2]
    scale = min(1.0, DETECTION_MAX_SIDE / float(max(height, width) or 1))
    sample = image if scale == 1.0 else cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.equalizeHist(cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY))
    minimum_ratio = 0.025 if retry else 0.035
    minimum = max(22 if retry else 28, int(min(gray.shape[:2]) * minimum_ratio))
    cascade_root = Path(cv2.data.haarcascades)
    detector_specs = [
        ("haarcascade_frontalface_alt2.xml", False),
        ("haarcascade_profileface.xml", True),
    ]
    if retry:
        detector_specs.extend([
            ("haarcascade_frontalface_default.xml", False),
            ("haarcascade_frontalface_alt.xml", False),
        ])
    detected: list[tuple[int, int, int, int]] = []
    for filename, detect_flipped in detector_specs:
        detector = cv2.CascadeClassifier(str(cascade_root / filename))
        if detector.empty():
            continue
        for x, y, w, h in detector.detectMultiScale(
            gray,
            scaleFactor=1.05 if retry else 1.08,
            minNeighbors=3 if retry else 4,
            minSize=(minimum, minimum),
            flags=cv2.CASCADE_SCALE_IMAGE,
        ):
            detected.append((int(x / scale), int(y / scale), int(w / scale), int(h / scale)))
        if detect_flipped:
            flipped = cv2.flip(gray, 1)
            for x, y, w, h in detector.detectMultiScale(
                flipped,
                scaleFactor=1.05 if retry else 1.08,
                minNeighbors=3 if retry else 4,
                minSize=(minimum, minimum),
                flags=cv2.CASCADE_SCALE_IMAGE,
            ):
                detected.append((int((gray.shape[1] - x - w) / scale), int(y / scale), int(w / scale), int(h / scale)))
    return _merge_faces(detected)


def _grid_region(region: np.ndarray, seed: str) -> np.ndarray:
    height, width = region.shape[:2]
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
    overlay = region.copy()
    scale = max(1.0, min(height, width) / 640.0)
    min_spacing = max(10, int(14 * scale))
    max_spacing = max(min_spacing + 2, int(26 * scale))

    x = int(rng.integers(0, max(1, min_spacing)))
    while x < width:
        line_width = max(2, int(rng.integers(2, 5) * scale))
        color = tuple(int(value) for value in rng.integers(20, 236, size=3))
        cv2.line(overlay, (x, 0), (x, height - 1), color, line_width, cv2.LINE_AA)
        x += int(rng.integers(min_spacing, max_spacing + 1))

    y = int(rng.integers(0, max(1, min_spacing)))
    while y < height:
        line_width = max(2, int(rng.integers(2, 5) * scale))
        color = tuple(int(value) for value in rng.integers(20, 236, size=3))
        cv2.line(overlay, (0, y), (width - 1, y), color, line_width, cv2.LINE_AA)
        y += int(rng.integers(min_spacing, max_spacing + 1))
    return cv2.addWeighted(overlay, GRID_ALPHA, region, 1.0 - GRID_ALPHA, 0)


def _apply_face_grids(image: np.ndarray, faces: list[tuple[int, int, int, int]], seed: str) -> np.ndarray:
    output = image.copy()
    image_height, image_width = output.shape[:2]
    for index, (x, y, width, height) in enumerate(faces):
        pad_x, pad_y = int(width * 0.2), int(height * 0.28)
        left, top = max(0, x - pad_x), max(0, y - pad_y)
        right, bottom = min(image_width, x + width + pad_x), min(image_height, y + height + pad_y)
        region = output[top:bottom, left:right]
        if region.size == 0:
            continue

        region_height, region_width = region.shape[:2]
        protected = _grid_region(region, f"{seed}:face:{index}:{left}:{top}:{right}:{bottom}")

        mask = np.zeros((region_height, region_width), dtype=np.uint8)
        cv2.ellipse(
            mask,
            (region_width // 2, region_height // 2),
            (max(1, region_width // 2 - 1), max(1, region_height // 2 - 1)),
            0,
            0,
            360,
            255,
            -1,
        )
        feather = max(5, (min(region_width, region_height) // 18) | 1)
        alpha = cv2.GaussianBlur(mask, (feather, feather), 0).astype(np.float32)[:, :, None] / 255.0
        output[top:bottom, left:right] = (protected * alpha + region * (1.0 - alpha)).astype(np.uint8)
    return output


def prepare_task_reference_images(task_id: str, retry_face_detection: bool | None = None) -> list[Path]:
    originals = task_image_paths(task_id)
    if not originals:
        return []

    try:
        meta = get_meta(task_id)
    except (FileNotFoundError, OSError):
        meta = {}
    if not bool(meta.get("reference_is_real_person")):
        try:
            update_meta(
                task_id,
                reference_face_detection_completed=False,
                reference_face_count=0,
                reference_face_processing_errors=[],
                reference_grid_mode="disabled",
            )
        except (FileNotFoundError, OSError):
            LOGGER.warning("could not persist disabled reference face metadata for task %s", task_id)
        return originals

    internal_dir = task_dir(task_id) / "processed_references"
    internal_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = internal_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    entries = manifest.setdefault("images", {})
    retry_face_detection = bool(meta.get("reference_face_grid_retry") or meta.get("reference_force_grid")) if retry_face_detection is None else bool(retry_face_detection)
    prepared: list[Path] = []
    total_faces = 0
    processing_errors: list[str] = []

    for index, source in enumerate(originals, start=1):
        fingerprint = _source_fingerprint(source)
        cached = entries.get(source.name) if isinstance(entries, dict) else None
        cached_mode = str(cached.get("mode") or "") if isinstance(cached, dict) else ""
        valid_modes = {"original-retry", "face-grid-retry"} if retry_face_detection else {"original", "face-grid"}
        cache_mode_matches = cached_mode in valid_modes
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint and cache_mode_matches:
            face_count = max(0, int(cached.get("face_count") or 0))
            processed_name = str(cached.get("processed_name") or "")
            processed_path = internal_dir / processed_name if processed_name else source
            if processed_path.exists():
                total_faces += face_count
                prepared.append(processed_path)
                continue

        image = _load_image(source)
        if image is None:
            processing_errors.append(source.name)
            mode = "original-retry" if retry_face_detection else "original"
            entries[source.name] = {"fingerprint": fingerprint, "face_count": 0, "processed_name": "", "mode": mode}
            prepared.append(source)
            continue

        faces = _detect_faces(image, retry=retry_face_detection)
        total_faces += len(faces)
        mode = ("face-grid-retry" if retry_face_detection else "face-grid") if faces else ("original-retry" if retry_face_detection else "original")
        processed_name = ""
        prepared_path = source
        if faces:
            protected = _apply_face_grids(image, faces, fingerprint)
            processed_name = f"{index:02d}-{fingerprint}-{mode}.jpg"
            prepared_path = internal_dir / processed_name
            success, encoded = cv2.imencode(".jpg", protected, [cv2.IMWRITE_JPEG_QUALITY, 93])
            if not success:
                raise RuntimeError(f"failed to encode protected reference image {source.name}")
            prepared_path.write_bytes(encoded.tobytes())
        entries[source.name] = {
            "fingerprint": fingerprint,
            "face_count": len(faces),
            "processed_name": processed_name,
            "mode": mode,
        }
        prepared.append(prepared_path)

    manifest["version"] = MANIFEST_VERSION
    _write_manifest(manifest_path, manifest)
    try:
        update_meta(
            task_id,
            reference_face_detection_completed=True,
            reference_face_count=total_faces,
            reference_face_processing_errors=processing_errors,
            reference_grid_mode=("face-grid-retry" if retry_face_detection else "face-grid") if total_faces else ("original-retry" if retry_face_detection else "original"),
        )
    except (FileNotFoundError, OSError):
        LOGGER.warning("could not persist reference face metadata for task %s", task_id)
    return prepared
