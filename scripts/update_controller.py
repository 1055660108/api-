from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path


APP_DIR = Path(os.environ.get("DOLA_UPDATE_APP_DIR", "/opt/dola-fetch-service")).resolve()
SOCKET_PATH = Path(os.environ.get("DOLA_UPDATE_SOCKET", "/run/dola-update/controller.sock"))
BRANCH = os.environ.get("DOLA_UPDATE_BRANCH", "main").strip() or "main"
EXPECTED_ORIGIN = os.environ.get("DOLA_UPDATE_REPOSITORY_URL", "https://github.com/1055660108/api-.git").strip()
STATE_LOCK = threading.Lock()
STATE: dict[str, str | bool] = {"updating": False, "phase": "空闲", "error": ""}


def run(*arguments: str, timeout: int = 900) -> str:
    result = subprocess.run(arguments, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout, check=False)
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(output[-1000:] or f"command exited with code {result.returncode}")
    return output


def git(*arguments: str, timeout: int = 120) -> str:
    return run("git", *arguments, timeout=timeout)


def set_state(**values: str | bool) -> None:
    with STATE_LOCK:
        STATE.update(values)


def status() -> dict[str, str | bool]:
    revision = git("rev-parse", "--short", "HEAD", timeout=15)
    with STATE_LOCK:
        current = dict(STATE)
    current.update({"repository": EXPECTED_ORIGIN, "branch": BRANCH, "revision": revision})
    return current


def wait_for_health() -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8088/health/live", timeout=3) as response:
                payload = json.load(response)
            api_id = run("docker", "compose", "ps", "-q", "api", timeout=15)
            worker_id = run("docker", "compose", "ps", "-q", "worker", timeout=15)
            api_health = run("docker", "inspect", "--format", "{{.State.Health.Status}}", api_id, timeout=15)
            worker_health = run("docker", "inspect", "--format", "{{.State.Health.Status}}", worker_id, timeout=15)
            if response.status == 200 and payload.get("ok") is True and api_health == "healthy" and worker_health == "healthy":
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("deployment health check timed out")


def deploy() -> None:
    before = ""
    try:
        set_state(phase="检查仓库", error="")
        if git("remote", "get-url", "origin", timeout=15).rstrip("/") != EXPECTED_ORIGIN.rstrip("/"):
            raise RuntimeError("repository origin does not match the configured update source")
        if git("status", "--porcelain", "--untracked-files=all", timeout=15):
            raise RuntimeError("application repository has uncommitted changes")
        before = git("rev-parse", "HEAD", timeout=15)
        run("docker", "image", "inspect", "dola-fetch-service:local", timeout=30)
        run("docker", "tag", "dola-fetch-service:local", "dola-fetch-service:rollback", timeout=30)
        set_state(phase="拉取代码")
        git("fetch", "--prune", "origin", BRANCH)
        target = git("rev-parse", f"origin/{BRANCH}", timeout=15)
        if before == target:
            set_state(updating=False, phase="已是最新", error="")
            return
        git("merge-base", "--is-ancestor", before, target, timeout=15)
        git("merge", "--ff-only", target)
        set_state(phase="构建镜像")
        run("docker", "compose", "build", "api", "worker")
        set_state(phase="更新服务")
        run("docker", "compose", "up", "-d", "--force-recreate", "api", "worker")
        set_state(phase="健康检查")
        wait_for_health()
        set_state(updating=False, phase="更新完成", error="", updated=True)
    except Exception as exc:
        error = str(exc)[:1000]
        if before:
            try:
                set_state(phase="正在回滚")
                git("reset", "--hard", before)
                run("docker", "tag", "dola-fetch-service:rollback", "dola-fetch-service:local", timeout=30)
                run("docker", "compose", "up", "-d", "--force-recreate", "api", "worker")
                wait_for_health()
            except Exception as rollback_exc:
                error = f"{error}; rollback failed: {str(rollback_exc)[:500]}"
        set_state(updating=False, phase="更新失败", error=error, updated=False)


def start_deploy() -> dict[str, str | bool]:
    with STATE_LOCK:
        if STATE["updating"]:
            raise RuntimeError("repository update is already running")
        STATE.update({"updating": True, "phase": "准备更新", "error": "", "updated": False})
    threading.Thread(target=deploy, daemon=True).start()
    return status()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code: int, payload: dict[str, str | bool]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/status":
            self.send_json(404, {"detail": "not found"})
            return
        try:
            self.send_json(200, status())
        except Exception as exc:
            self.send_json(409, {"detail": str(exc)[:1000]})

    def do_POST(self) -> None:
        if self.path != "/update":
            self.send_json(404, {"detail": "not found"})
            return
        try:
            self.send_json(202, start_deploy())
        except Exception as exc:
            self.send_json(409, {"detail": str(exc)[:1000]})

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("update controller must run as root")
    if not (APP_DIR / ".git").is_dir():
        raise SystemExit("application directory is not a Git repository")
    if not shutil.which("git") or not shutil.which("docker"):
        raise SystemExit("git and docker are required")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    with Server(str(SOCKET_PATH), Handler) as server:
        os.chown(SOCKET_PATH, 0, int(os.environ.get("DOLA_UPDATE_SOCKET_GID", "10001")))
        os.chmod(SOCKET_PATH, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
