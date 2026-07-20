from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse


REPOSITORY_URL = os.environ.get("DOLA_UPDATE_REPOSITORY_URL", "https://github.com/1055660108/api-.git").strip()
REPOSITORY_BRANCH = os.environ.get("DOLA_UPDATE_BRANCH", "main").strip() or "main"
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
    return f"https://github.com/{parsed.path.strip('/')}"


def repository_status(root: Path) -> dict[str, str | bool]:
    if not (root / ".git").exists():
        raise RuntimeError("application directory is not a Git repository")
    origin = _run_git(root, "remote", "get-url", "origin", timeout=15)
    if _normalized_repository(origin) != _normalized_repository(REPOSITORY_URL):
        raise RuntimeError("repository origin does not match the configured update source")
    revision = _run_git(root, "rev-parse", "--short", "HEAD", timeout=15)
    return {"repository": REPOSITORY_URL, "branch": REPOSITORY_BRANCH, "revision": revision, "updating": _UPDATE_LOCK.locked()}


def update_repository(root: Path) -> dict[str, str | bool]:
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise RuntimeError("repository update is already running")
    try:
        status = repository_status(root)
        before = str(status["revision"])
        _run_git(root, "fetch", "--prune", "origin", REPOSITORY_BRANCH)
        changed_files = _run_git(root, "diff", "--name-only", f"HEAD..origin/{REPOSITORY_BRANCH}", timeout=15).splitlines()
        local_changes = _run_git(root, "status", "--porcelain", "--untracked-files=all", timeout=15).splitlines()
        changed_paths = {line[3:].strip() for line in local_changes if len(line) > 3}
        conflicts = sorted(set(changed_files) & changed_paths)
        if conflicts:
            raise RuntimeError(f"local changes conflict with update: {', '.join(conflicts[:10])}")
        _run_git(root, "merge", "--ff-only", f"origin/{REPOSITORY_BRANCH}")
        after = _run_git(root, "rev-parse", "--short", "HEAD", timeout=15)
        return {
            "ok": True,
            "updated": before != after,
            "before_revision": before,
            "revision": after,
            "branch": REPOSITORY_BRANCH,
            "restart_required": before != after,
        }
    finally:
        _UPDATE_LOCK.release()
