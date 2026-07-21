from __future__ import annotations

import os
import json
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__


REPOSITORY_URL = os.environ.get("DOLA_UPDATE_REPOSITORY_URL", "https://github.com/1055660108/api-.git").strip()
REPOSITORY_BRANCH = os.environ.get("DOLA_UPDATE_BRANCH", "main").strip() or "main"
CONTROLLER_SOCKET = Path(os.environ.get("DOLA_UPDATE_CONTROLLER_SOCKET", "/run/dola-update/controller.sock"))
_UPDATE_LOCK = threading.Lock()


def _run_git(root: Path, *arguments: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(output[:500] or f"git exited with code {result.returncode}")
    return output


def _normalized_repository(value: str) -> str:
    repository = str(value or "").strip().rstrip("/")
    if repository.startswith("git@github.com:"):
        repository = f"https://github.com/{repository.split(':', 1)[1]}"
    parsed = urlparse(repository)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise RuntimeError("repository origin is not an allowed GitHub URL")
    return f"https://github.com/{parsed.path.strip('/')}".removesuffix(".git").lower()


def _revision_details(root: Path, revision: str) -> tuple[str, str]:
    short_revision = _run_git(root, "rev-parse", "--short", revision, timeout=15)
    commit_message = _run_git(root, "log", "-1", "--format=%s", revision, timeout=15)
    return short_revision, commit_message


def _revision_version(root: Path, revision: str) -> str:
    return _run_git(root, "show", f"{revision}:VERSION", timeout=15).strip()


def _controller_request(method: str, path: str) -> dict[str, str | bool]:
    try:
        transport = httpx.HTTPTransport(uds=str(CONTROLLER_SOCKET))
        with httpx.Client(transport=transport, timeout=15) as client:
            response = client.request(method, f"http://controller{path}")
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"deployment controller unavailable: {exc}") from exc
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("update controller returned an invalid response") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(str(payload.get("detail") or "update controller request failed"))
    return payload


def repository_status(root: Path) -> dict[str, str | bool]:
    if CONTROLLER_SOCKET.is_socket():
        return _controller_request("GET", "/status")
    if not (root / ".git").exists():
        raise RuntimeError("deployment controller is not installed; run scripts/install_update_controller.sh on a Docker deployment")
    origin = _run_git(root, "remote", "get-url", "origin", timeout=15)
    if _normalized_repository(origin) != _normalized_repository(REPOSITORY_URL):
        raise RuntimeError("repository origin does not match the configured update source")
    _run_git(root, "fetch", "--prune", "origin", REPOSITORY_BRANCH)
    revision, commit_message = _revision_details(root, "HEAD")
    latest_revision, latest_commit_message = _revision_details(root, f"origin/{REPOSITORY_BRANCH}")
    latest_version = _revision_version(root, f"origin/{REPOSITORY_BRANCH}")
    return {
        "repository": REPOSITORY_URL,
        "branch": REPOSITORY_BRANCH,
        "revision": revision,
        "version": __version__,
        "latest_version": latest_version,
        "commit_message": commit_message,
        "latest_revision": latest_revision,
        "latest_commit_message": latest_commit_message,
        "update_available": revision != latest_revision,
        "updating": _UPDATE_LOCK.locked(),
    }


def update_repository(root: Path) -> dict[str, str | bool]:
    if CONTROLLER_SOCKET.is_socket():
        return _controller_request("POST", "/update")
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise RuntimeError("repository update is already running")
    try:
        status = repository_status(root)
        before = str(status["revision"])
        changed_files = _run_git(root, "diff", "--name-only", f"HEAD..origin/{REPOSITORY_BRANCH}", timeout=15).splitlines()
        local_changes = _run_git(root, "status", "--porcelain", "--untracked-files=all", timeout=15).splitlines()
        changed_paths = {line[3:].strip() for line in local_changes if len(line) > 3}
        conflicts = sorted(set(changed_files) & changed_paths)
        if conflicts:
            raise RuntimeError(f"local changes conflict with update: {', '.join(conflicts[:10])}")
        _run_git(root, "merge", "--ff-only", f"origin/{REPOSITORY_BRANCH}")
        after, commit_message = _revision_details(root, "HEAD")
        return {
            "ok": True,
            "updated": before != after,
            "before_revision": before,
            "revision": after,
            "version": __version__,
            "latest_version": str(status.get("latest_version") or __version__),
            "commit_message": commit_message,
            "update_available": False,
            "branch": REPOSITORY_BRANCH,
            "restart_required": before != after,
        }
    finally:
        _UPDATE_LOCK.release()
