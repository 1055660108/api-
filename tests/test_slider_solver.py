from __future__ import annotations

import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import cv2
import numpy as np

from app import automation
from app.slider_solver import SliderChallengeSolver, SliderSolveResult
from app.slider_solver.cdp import find_slider_page
from app.slider_solver.image_solver import solve_gap
from app.slider_solver.motion import build_drag_path
from app.slider_solver.types import Box


class SliderImageSolverTests(unittest.TestCase):
    def test_gap_solver_locates_source_crop(self) -> None:
        rng = np.random.default_rng(20260731)
        background = rng.integers(0, 256, size=(60, 120, 3), dtype=np.uint8)
        source_x, source_y = 43, 17
        crop = background[source_y : source_y + 20, source_x : source_x + 20]
        alpha = np.full((20, 20, 1), 255, dtype=np.uint8)
        piece = np.concatenate([crop, alpha], axis=2)
        ok_background, background_bytes = cv2.imencode(".png", background)
        ok_piece, piece_bytes = cv2.imencode(".png", piece)

        self.assertTrue(ok_background)
        self.assertTrue(ok_piece)
        result = solve_gap(background_bytes.tobytes(), piece_bytes.tobytes())

        self.assertAlmostEqual(result.target_x, source_x + 3, delta=1)
        self.assertGreater(result.confidence, 0.9)

    def test_drag_path_finishes_at_exact_displacement(self) -> None:
        handle = Box(x=10, y=20, width=30, height=40)
        path = build_drag_path(handle, displacement=75, steps=12, overshoot=2.0)

        self.assertEqual(path[0], (25, 40))
        self.assertEqual(path[-1], (100, 40))
        self.assertGreater(max(point[0] for point in path), path[-1][0])


class SliderPageSolverTests(unittest.TestCase):
    def test_data_image_url_is_decoded_without_network_request(self) -> None:
        payload = b"fixture-image"
        url = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
        solver = SliderChallengeSolver()

        result = asyncio.run(solver._read_image(SimpleNamespace(context=None), None, url))

        self.assertEqual(result, payload)

    def test_browser_context_request_reads_authenticated_image(self) -> None:
        response = SimpleNamespace(ok=True, body=AsyncMock(return_value=b"image-bytes"))
        request = SimpleNamespace(get=AsyncMock(return_value=response))
        page = SimpleNamespace(context=SimpleNamespace(request=request), url="https://www.dola.com/chat")
        locator = SimpleNamespace(evaluate=AsyncMock())
        solver = SliderChallengeSolver()

        result = asyncio.run(solver._read_image(page, locator, "https://verify.example/image.png"))

        self.assertEqual(result, b"image-bytes")
        request.get.assert_awaited_once_with(
            "https://verify.example/image.png",
            headers={"referer": "https://www.dola.com/chat"},
            timeout=10_000,
        )
        locator.evaluate.assert_not_awaited()

    def test_find_slider_page_ignores_hidden_iframe(self) -> None:
        hidden = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        visible = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        locator = SimpleNamespace(
            count=AsyncMock(return_value=2),
            nth=Mock(side_effect=[hidden, visible]),
        )
        page = SimpleNamespace(url="https://www.dola.com/chat", locator=Mock(return_value=locator))
        context = SimpleNamespace(pages=[page])

        result = asyncio.run(find_slider_page([context], "iframe[src*='bdcaptcha.html']"))

        self.assertIs(result, page)


class SliderAutomationRecoveryTests(unittest.TestCase):
    @staticmethod
    def runner() -> automation.DolaFetchAutomation:
        runner = automation.DolaFetchAutomation.__new__(automation.DolaFetchAutomation)
        runner._set_phase = Mock()
        runner._inspect_service_frequent_account_state = AsyncMock()
        runner._resolve_slider_if_present = AsyncMock()
        return runner

    def test_submission_retries_in_same_page_after_successful_slider(self) -> None:
        runner = self.runner()
        runner._resolve_slider_if_present.return_value = SliderSolveResult(status="success", attempts=1)
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    {"slider_verification": True},
                    {"ok": True, "conversation_id": "123"},
                ]
            ),
            wait_for_timeout=AsyncMock(),
        )
        context = SimpleNamespace()

        result = asyncio.run(runner._submit_with_slider_recovery(page, context, {"prompt": "test"}))

        self.assertTrue(result["ok"])
        self.assertEqual(page.evaluate.await_count, 2)
        runner._resolve_slider_if_present.assert_awaited_once()
        runner._set_phase.assert_called_once_with("retrying_after_slider", "滑块验证已完成，正在重新提交")

    def test_submission_slider_recovery_can_reload_to_surface_hidden_challenge(self) -> None:
        runner = self.runner()
        runner.slider_enabled = True
        slider_page = SimpleNamespace()
        runner.slider_solver = SimpleNamespace(
            settings=SimpleNamespace(iframe_selector="iframe[src*='bdcaptcha.html']"),
            solve=AsyncMock(return_value=SliderSolveResult(status="success", attempts=1)),
        )
        runner._save_result = Mock()
        page = SimpleNamespace(reload=AsyncMock(), wait_for_timeout=AsyncMock())
        context = SimpleNamespace()

        with patch("app.automation.find_slider_page", new=AsyncMock(side_effect=[None, slider_page])):
            result = asyncio.run(
                automation.DolaFetchAutomation._resolve_slider_if_present(
                    runner,
                    page,
                    context,
                    phase="submission_response",
                    wait_seconds=0,
                    reload_if_missing=True,
                )
            )

        self.assertEqual(result.status, "success")
        page.reload.assert_awaited_once_with(wait_until="domcontentloaded", timeout=30000)
        runner.slider_solver.solve.assert_awaited_once_with(slider_page)

    def test_missing_submission_slider_records_diagnostic_after_reload(self) -> None:
        runner = self.runner()
        runner.slider_enabled = True
        runner.slider_solver = SimpleNamespace(
            settings=SimpleNamespace(iframe_selector="iframe[src*='bdcaptcha.html']"),
        )
        runner._save_result = Mock()
        page = SimpleNamespace(reload=AsyncMock(), wait_for_timeout=AsyncMock())

        with patch("app.automation.find_slider_page", new=AsyncMock(return_value=None)), patch(
            "app.automation.asyncio.get_running_loop"
        ) as get_loop:
            get_loop.return_value.time.side_effect = [0.0, 0.0, 0.0, 5.0]
            result = asyncio.run(
                automation.DolaFetchAutomation._resolve_slider_if_present(
                    runner,
                    page,
                    SimpleNamespace(),
                    phase="submission_response",
                    wait_seconds=0,
                    reload_if_missing=True,
                )
            )

        self.assertEqual(result.status, "not_present")
        saved = runner._save_result.call_args.kwargs["extra"]
        self.assertEqual(saved["slider_last_status"], "not_present")

    def test_submission_does_not_retry_when_slider_is_not_visible(self) -> None:
        runner = self.runner()
        runner._resolve_slider_if_present.return_value = SliderSolveResult(status="not_present", attempts=0)
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value={"slider_verification": True}),
            wait_for_timeout=AsyncMock(),
        )

        result = asyncio.run(runner._submit_with_slider_recovery(page, SimpleNamespace(), {}))

        self.assertTrue(result["slider_verification"])
        self.assertEqual(page.evaluate.await_count, 1)

    def test_service_frequent_slider_is_solved_before_retry(self) -> None:
        runner = self.runner()
        runner._inspect_service_frequent_account_state.return_value = {"state": "slider_verification"}
        runner._resolve_slider_if_present.return_value = SliderSolveResult(status="success", attempts=1)
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    {"service_frequent": True},
                    {"ok": True, "conversation_id": "456"},
                ]
            ),
            wait_for_timeout=AsyncMock(),
        )

        result = asyncio.run(runner._submit_with_slider_recovery(page, SimpleNamespace(), {}))

        self.assertTrue(result["ok"])
        self.assertEqual(page.evaluate.await_count, 2)
        runner._inspect_service_frequent_account_state.assert_awaited_once()

    def test_environment_slider_settings_are_bounded(self) -> None:
        with patch.dict(
            automation.os.environ,
            {
                "DOLA_SLIDER_MAX_ATTEMPTS": "99",
                "DOLA_SLIDER_VERIFY_TIMEOUT_SECONDS": "0",
                "DOLA_SLIDER_MINIMUM_CONFIDENCE": "2",
            },
        ):
            self.assertEqual(
                automation._environment_int("DOLA_SLIDER_MAX_ATTEMPTS", 3, minimum=1, maximum=8),
                8,
            )
            self.assertEqual(
                automation._environment_float(
                    "DOLA_SLIDER_VERIFY_TIMEOUT_SECONDS", 5.0, minimum=1.0, maximum=30.0
                ),
                1.0,
            )
            self.assertEqual(
                automation._environment_float(
                    "DOLA_SLIDER_MINIMUM_CONFIDENCE", 0.45, minimum=0.0, maximum=1.0
                ),
                1.0,
            )


if __name__ == "__main__":
    unittest.main()
