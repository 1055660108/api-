from __future__ import annotations

import json
import os
import signal
import sys
import urllib.request
from pathlib import Path


HEALTH_URL = os.environ.get("DOLA_API_HEALTH_URL", "http://127.0.0.1:8088/health/live")
FAILURE_STATE_PATH = Path(os.environ.get("DOLA_API_HEALTH_STATE_PATH", "/tmp/dola-api-health-failures"))


def _environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _process_start_ticks(process: Path) -> str:
    try:
        value = (process / "stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return ""
    closing = value.rfind(")")
    fields = value[closing + 1 :].split() if closing >= 0 else []
    return fields[19] if len(fields) > 19 else ""


def api_process_snapshot(proc_root: Path = Path("/proc")) -> tuple[str, int] | None:
    try:
        processes = tuple(proc_root.iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes().split(b"\0")
            executable = (process / "comm").read_text(encoding="utf-8").strip().lower()
            is_api = executable.startswith("python") and any(Path(part.decode(errors="ignore")).name == "run.py" for part in command if part)
            if is_api:
                start_ticks = _process_start_ticks(process)
                identity = f"{process.name}:{start_ticks or 'unknown'}"
                return identity, sum(1 for _ in (process / "fd").iterdir())
        except (OSError, UnicodeError):
            continue
    return None


def api_process_fd_count(proc_root: Path = Path("/proc")) -> int | None:
    snapshot = api_process_snapshot(proc_root)
    return snapshot[1] if snapshot else None


def api_process_identity(proc_root: Path = Path("/proc")) -> str:
    snapshot = api_process_snapshot(proc_root)
    if snapshot:
        return snapshot[0]
    start_ticks = _process_start_ticks(proc_root / "1")
    return f"container:{start_ticks}" if start_ticks else "unavailable"


def probe_api() -> tuple[bool, str]:
    timeout = _environment_int("DOLA_API_HEALTH_TIMEOUT_SECONDS", 4, 1, 4)
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            payload = json.load(response)
        if response.status != 200 or payload.get("ok") is not True:
            return False, "health endpoint returned an invalid response"
    except Exception as exc:
        return False, f"health endpoint unavailable: {exc}"

    fd_count = api_process_fd_count()
    threshold = _environment_int("DOLA_API_FD_RESTART_THRESHOLD", 4096, 256, 65535)
    if fd_count is None:
        return False, "API process was not found"
    if fd_count >= threshold:
        return False, f"API file descriptors reached {fd_count}/{threshold}"
    return True, f"ok fd={fd_count}"


def _failure_state() -> tuple[str, int]:
    try:
        raw = FAILURE_STATE_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return "", 0
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            return "", max(0, int(raw))
        except ValueError:
            return "", 0
    if not isinstance(value, dict):
        return "", 0
    try:
        failures = max(0, int(value.get("failures") or 0))
    except (TypeError, ValueError):
        failures = 0
    return str(value.get("process_identity") or ""), failures


def _record_failure(process_identity: str) -> int:
    previous_identity, previous_count = _failure_state()
    count = previous_count + 1 if previous_identity == process_identity else 1
    try:
        FAILURE_STATE_PATH.write_text(
            json.dumps({"process_identity": process_identity, "failures": count}, separators=(",", ":")),
            encoding="ascii",
        )
    except OSError:
        pass
    return count


def _clear_failures() -> None:
    try:
        FAILURE_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    process_identity = api_process_identity()
    healthy, detail = probe_api()
    if healthy:
        _clear_failures()
        return 0

    failures = _record_failure(process_identity)
    restart_after = _environment_int("DOLA_API_HEALTH_FAILURES_BEFORE_RESTART", 5, 2, 10)
    print(
        f"API health check failed ({failures}/{restart_after}, process={process_identity}): {detail}",
        file=sys.stderr,
        flush=True,
    )
    if failures >= restart_after:
        try:
            os.kill(1, signal.SIGTERM)
        except OSError as exc:
            print(f"failed to terminate unhealthy container: {exc}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
