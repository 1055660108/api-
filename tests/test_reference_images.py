from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from app import reference_images


class ReferenceImageTests(unittest.TestCase):
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
            ), patch.object(reference_images, "update_meta"), patch.object(reference_images, "_detect_faces", return_value=[]):
                prepared = reference_images.prepare_task_reference_images("0" * 32)

            self.assertEqual(prepared, [source])

    def test_force_grid_processes_image_without_detected_face_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "01.jpg"
            success, encoded = cv2.imencode(".jpg", np.full((120, 160, 3), 180, dtype=np.uint8))
            self.assertTrue(success)
            original = encoded.tobytes()
            source.write_bytes(original)

            with patch.object(reference_images, "task_image_paths", return_value=[source]), patch.object(
                reference_images, "task_dir", return_value=root
            ), patch.object(reference_images, "update_meta") as update_meta, patch.object(
                reference_images, "_detect_faces", return_value=[]
            ):
                prepared = reference_images.prepare_task_reference_images("0" * 32, force_grid=True)

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(prepared[0].parent.name, "processed_references")
            self.assertNotEqual(prepared[0].read_bytes(), original)
            self.assertEqual(update_meta.call_args.kwargs["reference_grid_mode"], "full-grid")
