from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .store import get_meta, task_dir, task_image_paths, update_meta


LOGGER = logging.getLogger(__name__)
DETECTION_MAX_SIDE = 1600
MANIFEST_VERSION = 3
GRID_ALPHA = 0.5
GRID_COLOR = (255, 255, 255)
GRID_LINE_WIDTH = 2
REFERENCE_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
REFERENCE_UPLOAD_MAX_SIDE = 3072
REFERENCE_UPLOAD_JPEG_QUALITIES = (92, 88, 84, 80, 76, 72, 68, 64)


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


def _encode_upload_copy(image: np.ndarray) -> bytes | None:
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return None
    best: bytes | None = None
    initial_side = min(REFERENCE_UPLOAD_MAX_SIDE, max(height, width))
    target_sides = list(dict.fromkeys((initial_side, 2560, 2048, 1600, 1280, 1024, 768)))
    for max_side in target_sides:
        if max_side > initial_side:
            continue
        scale = min(1.0, max_side / float(max(height, width)))
        prepared = image if scale == 1.0 else cv2.resize(
            image,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
        for quality in REFERENCE_UPLOAD_JPEG_QUALITIES:
            success, encoded = cv2.imencode(
                ".jpg",
                prepared,
                [cv2.IMWRITE_JPEG_QUALITY, quality, cv2.IMWRITE_JPEG_OPTIMIZE, 1],
            )
            if not success:
                continue
            candidate = encoded.tobytes()
            if best is None or len(candidate) < len(best):
                best = candidate
            if len(candidate) <= REFERENCE_UPLOAD_MAX_BYTES:
                return candidate
    return best


def _prepare_upload_sized_images(task_id: str, paths: list[Path]) -> list[Path]:
    if not paths:
        return []
    upload_dir = task_dir(task_id) / "processed_references" / "upload_ready"
    prepared: list[Path] = []
    optimized_count = 0
    original_bytes = 0
    prepared_bytes = 0
    errors: list[str] = []
    for source in paths:
        try:
            source_size = source.stat().st_size
        except OSError:
            prepared.append(source)
            errors.append(source.name)
            continue
        original_bytes += source_size
        if source_size <= REFERENCE_UPLOAD_MAX_BYTES:
            prepared.append(source)
            prepared_bytes += source_size
            continue
        fingerprint = _source_fingerprint(source)
        target = upload_dir / f"{fingerprint}.jpg"
        if target.is_file() and 0 < target.stat().st_size <= REFERENCE_UPLOAD_MAX_BYTES:
            prepared.append(target)
            prepared_bytes += target.stat().st_size
            optimized_count += 1
            continue
        image = _load_image(source)
        encoded = _encode_upload_copy(image) if image is not None else None
        if not encoded:
            prepared.append(source)
            prepared_bytes += source_size
            errors.append(source.name)
            continue
        upload_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(target)
        prepared.append(target)
        prepared_bytes += len(encoded)
        optimized_count += 1
    try:
        update_meta(
            task_id,
            reference_upload_optimized_count=optimized_count,
            reference_upload_original_bytes=original_bytes,
            reference_upload_prepared_bytes=prepared_bytes,
            reference_upload_optimization_errors=errors,
        )
    except (FileNotFoundError, OSError):
        LOGGER.warning("could not persist reference upload optimization metadata for task %s", task_id)
    return prepared


def _promote_prepared_references(task_id: str, originals: list[Path], prepared: list[Path]) -> list[Path]:
    if not originals or len(originals) != len(prepared):
        return prepared
    try:
        meta = get_meta(task_id)
    except (FileNotFoundError, OSError):
        meta = {}
    finalized_names = {
        str(name or "").strip()
        for name in meta.get("reference_finalized_image_names") or []
        if str(name or "").strip()
    }
    promoted: list[Path] = []
    deleted_count = 0
    reclaimed_bytes = 0
    cleanup_errors: list[str] = []
    for index, (original, ready) in enumerate(zip(originals, prepared, strict=True), start=1):
        if ready == original:
            promoted.append(original)
            continue
        try:
            original_size = original.stat().st_size
            ready_size = ready.stat().st_size
            if ready_size <= 0:
                raise OSError("prepared reference is empty")
        except OSError:
            promoted.append(original)
            cleanup_errors.append(original.name)
            continue

        target = original.parent / f"{index:02d}-compressed.jpg"
        try:
            if ready != target:
                ready.replace(target)
            if original != target:
                original.unlink()
        except OSError:
            if target != original:
                target.unlink(missing_ok=True)
            promoted.append(original)
            cleanup_errors.append(original.name)
            continue

        promoted.append(target)
        finalized_names.add(target.name)
        deleted_count += 1
        reclaimed_bytes += max(0, original_size - ready_size)

    if deleted_count and all(path.parent == originals[0].parent for path in promoted):
        shutil.rmtree(task_dir(task_id) / "processed_references", ignore_errors=True)
    if deleted_count or cleanup_errors:
        try:
            update_meta(
                task_id,
                reference_finalized_image_names=sorted(finalized_names),
                reference_originals_deleted_count=(
                    int(meta.get("reference_originals_deleted_count") or 0) + deleted_count
                ),
                reference_original_bytes_reclaimed=(
                    int(meta.get("reference_original_bytes_reclaimed") or 0) + reclaimed_bytes
                ),
                reference_original_cleanup_errors=cleanup_errors,
            )
        except (FileNotFoundError, OSError):
            LOGGER.warning("could not persist reference cleanup metadata for task %s", task_id)
    return promoted


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
    overlay = region.copy()
    spacing = max(18, int(round(min(height, width) / 20.0)))
    phase = int.from_bytes(digest[:2], "big") % spacing

    for offset in range(-height + phase, width + spacing, spacing):
        cv2.line(
            overlay,
            (offset, 0),
            (offset + height, height),
            GRID_COLOR,
            GRID_LINE_WIDTH,
            cv2.LINE_AA,
        )
    for offset in range(phase, width + height + spacing, spacing):
        cv2.line(
            overlay,
            (offset, 0),
            (offset - height, height),
            GRID_COLOR,
            GRID_LINE_WIDTH,
            cv2.LINE_AA,
        )
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
        prepared = _prepare_upload_sized_images(task_id, originals)
        return _promote_prepared_references(task_id, originals, prepared)

    finalized_names = {
        str(name or "").strip()
        for name in meta.get("reference_finalized_image_names") or []
        if str(name or "").strip()
    }
    if all(source.name in finalized_names for source in originals):
        upload_ready = _prepare_upload_sized_images(task_id, originals)
        return _promote_prepared_references(task_id, originals, upload_ready)

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
        if source.name in finalized_names:
            prepared.append(source)
            continue
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
    upload_ready = _prepare_upload_sized_images(task_id, prepared)
    return _promote_prepared_references(task_id, originals, upload_ready)
