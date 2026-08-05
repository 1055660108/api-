from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from typing import Any

import httpx


class SemanticCaptchaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CoordinateSolution:
    coordinates: list[list[float]]
    cost: str = ""


class AntiCaptchaCoordinateSolver:
    supports_rectangles = True

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = "https://api.anti-captcha.com",
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://api.anti-captcha.com").strip().rstrip("/")
        self.timeout_seconds = max(15.0, min(300.0, float(timeout_seconds or 120.0)))
        self.transport = transport

    @classmethod
    def from_environment(cls) -> "AntiCaptchaCoordinateSolver":
        raw_timeout = str(os.environ.get("DOUBAO_SEMANTIC_CAPTCHA_TIMEOUT_SECONDS") or "120")
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 120.0
        return cls(
            os.environ.get("DOUBAO_SEMANTIC_CAPTCHA_API_KEY", ""),
            base_url=os.environ.get(
                "DOUBAO_SEMANTIC_CAPTCHA_API_BASE_URL",
                "https://api.anti-captcha.com",
            ),
            timeout_seconds=timeout,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def balance(self) -> float:
        if not self.enabled:
            raise SemanticCaptchaError("semantic captcha API key is not configured")
        payload = await self._post("/getBalance", {"clientKey": self.api_key})
        return float(payload.get("balance") or 0.0)

    async def solve(
        self,
        image: bytes,
        *,
        comment: str,
        mode: str,
    ) -> CoordinateSolution:
        if not self.enabled:
            raise SemanticCaptchaError("semantic captcha API key is not configured")
        if not image:
            raise SemanticCaptchaError("semantic captcha screenshot is empty")
        normalized_mode = "rectangles" if str(mode or "").lower() == "rectangles" else "points"
        created = await self._post(
            "/createTask",
            {
                "clientKey": self.api_key,
                "task": {
                    "type": "ImageToCoordinatesTask",
                    "body": base64.b64encode(image).decode("ascii"),
                    "comment": str(comment or "Follow the instruction shown in the image")[:1000],
                    "mode": normalized_mode,
                    "websiteURL": "https://www.doubao.com/",
                },
                "softId": 0,
            },
        )
        task_id = int(created.get("taskId") or 0)
        if task_id <= 0:
            raise SemanticCaptchaError("semantic captcha service did not return a task id")

        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            result = await self._post(
                "/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
            )
            if str(result.get("status") or "") == "processing":
                continue
            if str(result.get("status") or "") != "ready":
                raise SemanticCaptchaError("semantic captcha service returned an invalid status")
            solution = result.get("solution")
            coordinates = solution.get("coordinates") if isinstance(solution, dict) else None
            normalized = self._normalize_coordinates(coordinates, normalized_mode)
            return CoordinateSolution(normalized, str(result.get("cost") or ""))
        raise SemanticCaptchaError("semantic captcha service timed out")

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            try:
                response = await client.post(path, json=payload)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SemanticCaptchaError(f"semantic captcha service request failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise SemanticCaptchaError("semantic captcha service returned an invalid response")
        if int(data.get("errorId") or 0) != 0:
            code = str(data.get("errorCode") or "semantic captcha service error")
            description = str(data.get("errorDescription") or "")
            raise SemanticCaptchaError(f"{code}: {description}"[:300])
        return data

    @staticmethod
    def _normalize_coordinates(value: Any, mode: str) -> list[list[float]]:
        width = 4 if mode == "rectangles" else 2
        if not isinstance(value, list):
            raise SemanticCaptchaError("semantic captcha service returned no coordinates")
        result: list[list[float]] = []
        for item in value[:12]:
            if not isinstance(item, (list, tuple)) or len(item) != width:
                continue
            try:
                coordinate = [float(part) for part in item]
            except (TypeError, ValueError):
                continue
            if all(part >= 0 for part in coordinate):
                result.append(coordinate)
        if not result:
            raise SemanticCaptchaError("semantic captcha service returned invalid coordinates")
        return result


class TwoCaptchaCoordinateSolver(AntiCaptchaCoordinateSolver):
    supports_rectangles = False

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = "https://api.2captcha.com",
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    async def solve(
        self,
        image: bytes,
        *,
        comment: str,
        mode: str,
    ) -> CoordinateSolution:
        if not self.enabled:
            raise SemanticCaptchaError("semantic captcha API key is not configured")
        if not image:
            raise SemanticCaptchaError("semantic captcha screenshot is empty")
        created = await self._post(
            "/createTask",
            {
                "clientKey": self.api_key,
                "task": {
                    "type": "CoordinatesTask",
                    "body": base64.b64encode(image).decode("ascii"),
                    "comment": str(comment or "Follow the instruction shown in the image")[:1000],
                    "minClicks": 1,
                    "maxClicks": 6,
                },
            },
        )
        task_id = int(created.get("taskId") or 0)
        if task_id <= 0:
            raise SemanticCaptchaError("semantic captcha service did not return a task id")

        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)
            result = await self._post(
                "/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
            )
            if str(result.get("status") or "") == "processing":
                continue
            if str(result.get("status") or "") != "ready":
                raise SemanticCaptchaError("semantic captcha service returned an invalid status")
            solution = result.get("solution")
            coordinates = solution.get("coordinates") if isinstance(solution, dict) else None
            normalized: list[list[float]] = []
            if isinstance(coordinates, list):
                for item in coordinates[:12]:
                    if isinstance(item, dict):
                        item = [item.get("x"), item.get("y")]
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    try:
                        point = [float(item[0]), float(item[1])]
                    except (TypeError, ValueError):
                        continue
                    if all(part >= 0 for part in point):
                        normalized.append(point)
            if not normalized:
                raise SemanticCaptchaError("semantic captcha service returned invalid coordinates")
            return CoordinateSolution(normalized, str(result.get("cost") or ""))
        raise SemanticCaptchaError("semantic captcha service timed out")


def coordinate_solver_from_environment() -> AntiCaptchaCoordinateSolver:
    provider = str(
        os.environ.get("DOUBAO_SEMANTIC_CAPTCHA_PROVIDER") or "2captcha"
    ).strip().lower()
    api_key = os.environ.get("DOUBAO_SEMANTIC_CAPTCHA_API_KEY", "")
    base_url = str(os.environ.get("DOUBAO_SEMANTIC_CAPTCHA_API_BASE_URL") or "").strip()
    raw_timeout = str(os.environ.get("DOUBAO_SEMANTIC_CAPTCHA_TIMEOUT_SECONDS") or "120")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = 120.0
    if provider in {"2captcha", "two_captcha", "twocaptcha"}:
        return TwoCaptchaCoordinateSolver(
            api_key,
            base_url=base_url or "https://api.2captcha.com",
            timeout_seconds=timeout,
        )
    if provider in {"anti_captcha", "anticaptcha", "anti-captcha"}:
        return AntiCaptchaCoordinateSolver(
            api_key,
            base_url=base_url or "https://api.anti-captcha.com",
            timeout_seconds=timeout,
        )
    raise SemanticCaptchaError(f"unsupported semantic captcha provider: {provider}")
