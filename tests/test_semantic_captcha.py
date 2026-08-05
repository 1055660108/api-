from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.semantic_captcha import AntiCaptchaCoordinateSolver, SemanticCaptchaError


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
