from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from app import reference_images


class ReferenceImageTests(unittest.TestCase):
    def test_face_grid_uses_two_pixel_half_transparent_white_crosshatch(self) -> None:
        region = np.zeros((120, 140, 3), dtype=np.uint8)
        processed = reference_images._grid_region(region, "white-grid")

        self.assertEqual(reference_images.GRID_ALPHA, 0.5)
        self.assertEqual(reference_images.GRID_COLOR, (255, 255, 255))
        self.assertEqual(reference_images.GRID_LINE_WIDTH, 2)
        self.assertTrue(np.array_equal(processed[:, :, 0], processed[:, :, 1]))
        self.assertTrue(np.array_equal(processed[:, :, 1], processed[:, :, 2]))
        self.assertGreaterEqual(int(processed.max()), 127)
        self.assertLessEqual(int(processed.max()), 128)
        self.assertGreater(np.count_nonzero(processed), 0)

    def test_face_processing_keeps_original_and_reuses_internal_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01.png"
            image = np.full((160, 160, 3), 220, dtype=np.uint8)
            image[45:115, 50:110] = (80, 130, 190)
            success, encoded = cv2.imencode(".png", image)
            self.assertTrue(success)
            original = encoded.tobytes()
            source.write_bytes(original)

            with patch.object(reference_images, "task_image_paths", return_value=[source]), patch.object(
                reference_images, "task_dir", return_value=root
            ), patch.object(
                reference_images, "get_meta", return_value={"reference_is_real_person": True}
            ), patch.object(reference_images, "update_meta") as update_meta, patch.object(
                reference_images, "_detect_faces", return_value=[(50, 45, 60, 70)]
            ) as detect:
                first = reference_images.prepare_task_reference_images("0" * 32)
                second = reference_images.prepare_task_reference_images("0" * 32)

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(first, second)
            self.assertEqual(first[0].parent.name, "processed_references")
            self.assertNotEqual(first[0].read_bytes(), original)
            self.assertEqual(detect.call_count, 1)
            self.assertEqual(update_meta.call_count, 2)
            self.assertEqual(update_meta.call_args.kwargs["reference_face_count"], 1)

    def test_image_without_detected_face_is_uploaded_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01.jpg"
            success, encoded = cv2.imencode(".jpg", np.zeros((80, 80, 3), dtype=np.uint8))
            self.assertTrue(success)
            source.write_bytes(encoded.tobytes())

            with patch.object(reference_images, "task_image_paths", return_value=[source]), patch.object(
                reference_images, "task_dir", return_value=root
            ), patch.object(
                reference_images, "get_meta", return_value={"reference_is_real_person": True}
            ), patch.object(reference_images, "update_meta"), patch.object(reference_images, "_detect_faces", return_value=[]):
                prepared = reference_images.prepare_task_reference_images("0" * 32)

            self.assertEqual(prepared, [source])

    def test_retry_only_grids_detected_face_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01.jpg"
            success, encoded = cv2.imencode(".jpg", np.full((120, 160, 3), 180, dtype=np.uint8))
            self.assertTrue(success)
            original = encoded.tobytes()
            source.write_bytes(original)

            with patch.object(reference_images, "task_image_paths", return_value=[source]), patch.object(
                reference_images, "task_dir", return_value=root
            ), patch.object(
                reference_images, "get_meta", return_value={"reference_is_real_person": True}
            ), patch.object(reference_images, "update_meta") as update_meta, patch.object(
                reference_images, "_detect_faces", return_value=[(50, 35, 60, 70)]
            ) as detect:
                prepared = reference_images.prepare_task_reference_images("0" * 32, retry_face_detection=True)

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(prepared[0].parent.name, "processed_references")
            self.assertNotEqual(prepared[0].read_bytes(), original)
            self.assertTrue(detect.call_args.kwargs["retry"])
            self.assertEqual(update_meta.call_args.kwargs["reference_grid_mode"], "face-grid-retry")

    def test_retry_without_detected_face_still_uses_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01.jpg"
            success, encoded = cv2.imencode(".jpg", np.full((120, 160, 3), 180, dtype=np.uint8))
            self.assertTrue(success)
            source.write_bytes(encoded.tobytes())

            with patch.object(reference_images, "task_image_paths", return_value=[source]), patch.object(
                reference_images, "task_dir", return_value=root
            ), patch.object(
                reference_images, "get_meta", return_value={"reference_is_real_person": True}
            ), patch.object(reference_images, "update_meta") as update_meta, patch.object(
                reference_images, "_detect_faces", return_value=[]
            ):
                prepared = reference_images.prepare_task_reference_images("0" * 32, retry_face_detection=True)

            self.assertEqual(prepared, [source])
            self.assertEqual(update_meta.call_args.kwargs["reference_grid_mode"], "original-retry")

    def test_unchecked_reference_skips_face_detection_and_processing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01.png"
            source.write_bytes(b"original-reference")

            with patch.object(reference_images, "task_image_paths", return_value=[source]), patch.object(
                reference_images, "task_dir", return_value=root
            ), patch.object(
                reference_images, "get_meta", return_value={"reference_is_real_person": False}
            ), patch.object(reference_images, "update_meta") as update_meta, patch.object(
                reference_images, "_load_image"
            ) as load_image, patch.object(reference_images, "_detect_faces") as detect_faces:
                prepared = reference_images.prepare_task_reference_images("0" * 32)

            self.assertEqual(prepared, [source])
            self.assertFalse((root / "processed_references").exists())
            load_image.assert_not_called()
            detect_faces.assert_not_called()
            self.assertEqual(update_meta.call_args.kwargs["reference_grid_mode"], "disabled")
            self.assertFalse(update_meta.call_args.kwargs["reference_face_detection_completed"])

    def test_face_grid_does_not_modify_pixels_outside_face_region(self) -> None:
        image = np.full((180, 220, 3), 160, dtype=np.uint8)
        processed = reference_images._apply_face_grids(image, [(80, 55, 60, 70)], "face-only")

        self.assertTrue(np.array_equal(processed[:20, :], image[:20, :]))
        self.assertTrue(np.array_equal(processed[:, :40], image[:, :40]))
        self.assertGreater(np.count_nonzero(processed[40:145, 68:152] != image[40:145, 68:152]), 0)
