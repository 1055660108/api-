from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.semantic_captcha import AntiCaptchaCoordinateSolver, SemanticCaptchaError, TwoCaptchaCoordinateSolver, coordinate_solver_from_environment


class SemanticCaptchaTests(unittest.TestCase):
    def test_coordinate_solver_creates_and_polls_rectangle_task(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/createTask":
                body = request.read().decode("utf-8")
                self.assertIn('"mode":"rectangles"', body)
                self.assertNotIn("secret-image", body)
                return httpx.Response(200, json={"errorId": 0, "taskId": 42})
            return httpx.Response(
                200,
                json={
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"coordinates": [[10, 20, 30, 40], [50, 60, 70, 80]]},
                    "cost": "0.000700",
                },
            )

        solver = AntiCaptchaCoordinateSolver(
            "token",
            base_url="https://solver.example",
            transport=httpx.MockTransport(handler),
        )
        with patch("app.semantic_captcha.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(
                solver.solve(b"secret-image", comment="Select animals", mode="rectangles")
            )

        self.assertEqual(result.coordinates, [[10.0, 20.0, 30.0, 40.0], [50.0, 60.0, 70.0, 80.0]])
        self.assertEqual(result.cost, "0.000700")
        self.assertEqual(calls, ["/createTask", "/getTaskResult"])

    def test_coordinate_solver_requires_api_key(self) -> None:
        solver = AntiCaptchaCoordinateSolver()
        with self.assertRaisesRegex(SemanticCaptchaError, "API key"):
            asyncio.run(solver.solve(b"image", comment="Select", mode="points"))

    def test_coordinate_solver_rejects_invalid_coordinates(self) -> None:
        with self.assertRaisesRegex(SemanticCaptchaError, "invalid coordinates"):
            AntiCaptchaCoordinateSolver._normalize_coordinates([[1, 2]], "rectangles")

    def test_two_captcha_solver_returns_point_coordinates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/createTask":
                body = request.read().decode("utf-8")
                self.assertIn('"type":"CoordinatesTask"', body)
                self.assertIn('"maxClicks":6', body)
                return httpx.Response(200, json={"errorId": 0, "taskId": 84})
            return httpx.Response(
                200,
                json={
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"coordinates": [{"x": 125, "y": 240}, {"x": 310, "y": 410}]},
                    "cost": "0.0012",
                },
            )

        solver = TwoCaptchaCoordinateSolver(
            "token",
            transport=httpx.MockTransport(handler),
        )
        with patch("app.semantic_captcha.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(
                solver.solve(b"image", comment="Select animals", mode="rectangles")
            )

        self.assertFalse(solver.supports_rectangles)
        self.assertEqual(result.coordinates, [[125.0, 240.0], [310.0, 410.0]])
        self.assertEqual(result.cost, "0.0012")

    def test_environment_factory_defaults_to_two_captcha(self) -> None:
        with patch.dict("os.environ", {"DOUBAO_SEMANTIC_CAPTCHA_API_KEY": "token"}, clear=True):
            solver = coordinate_solver_from_environment()

        self.assertIsInstance(solver, TwoCaptchaCoordinateSolver)
        self.assertTrue(solver.enabled)
