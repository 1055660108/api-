from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.validation_common import compose_command, require_test_environment, run_checked, run_process, utc_now, wait_until, write_report


SERVICES = ("redis", "postgres", "worker")


def compose(compose_file: Path, *arguments: str, timeout: float = 120) -> str:
    return run_checked(compose_command(compose_file, *arguments), compose_file.parent, timeout)


def service_state(compose_file: Path, service: str) -> dict[str, str]:
    container_id = compose(compose_file, "ps", "-q", service)
    if not container_id:
        return {"status": "missing", "health": "missing", "container_id": ""}
    output = run_checked(["docker", "inspect", "--format", "{{json .State}}", container_id], compose_file.parent)
    state = json.loads(output)
    return {"status": str(state.get("Status") or ""), "health": str((state.get("Health") or {}).get("Status") or "none"), "container_id": container_id}


def service_ready(compose_file: Path, service: str) -> bool:
    state = service_state(compose_file, service)
    return state["status"] == "running" and state["health"] in {"healthy", "none"}


def http_probe(base_url: str, token: str = "") -> dict[str, Any]:
    headers = {"User-Agent": "dola-test-fault-injection/1.0"}
    if token:
        headers["X-API-Token"] = token
    path = "/health" if token else "/health/live"
    started = time.perf_counter()
    try:
        with urlopen(Request(f"{base_url.rstrip('/')}{path}", headers=headers), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {"reachable": True, "status": response.status, "latency_ms": round((time.perf_counter() - started) * 1000, 3), "payload": payload}
    except HTTPError as exc:
        return {"reachable": True, "status": exc.code, "latency_ms": round((time.perf_counter() - started) * 1000, 3), "error": str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"reachable": False, "status": 0, "latency_ms": round((time.perf_counter() - started) * 1000, 3), "error": str(exc)}


def dependency_probe(compose_file: Path, service: str) -> bool:
    if service == "redis":
        return compose(compose_file, "exec", "-T", "redis", "redis-cli", "ping").strip() == "PONG"
    if service == "postgres":
        command = "pg_isready -U \"${POSTGRES_USER:-dola}\" -d \"${POSTGRES_DB:-dola}\""
        return "accepting connections" in compose(compose_file, "exec", "-T", "postgres", "sh", "-c", command)
    return service_ready(compose_file, "worker")


def inject_and_restore(compose_file: Path, service: str, base_url: str, token: str, timeout: float, settle: float) -> dict[str, Any]:
    before = service_state(compose_file, service)
    if before["status"] != "running":
        raise RuntimeError(f"服务 {service} 未运行，不能执行故障注入")
    event: dict[str, Any] = {"service": service, "started_at": utc_now(), "before": before}
    restored = False
    try:
        compose(compose_file, "stop", "-t", "0", service)
        stopped, stopped_state = wait_until(lambda: service_state(compose_file, service)["status"] in {"exited", "missing"}, min(timeout, 30), 0.5)
        event["fault"] = {"observed": stopped, "state": service_state(compose_file, service), "api": http_probe(base_url, token)}
        if not stopped:
            raise RuntimeError(f"未观察到 {service} 停止：{stopped_state}")
    finally:
        compose(compose_file, "up", "-d", service, timeout=max(timeout, 120))
        restored, last = wait_until(lambda: service_ready(compose_file, service), timeout, 1)
        event["recovery"] = {"observed": restored, "state": service_state(compose_file, service), "last": last}
    if restored:
        time.sleep(settle)
        dependency_ok, dependency_last = wait_until(lambda: dependency_probe(compose_file, service), min(timeout, 30), 1)
        api = http_probe(base_url, token)
        event["recovery"].update({"dependency_probe": dependency_ok, "dependency_last": dependency_last, "api": api})
        event["passed"] = dependency_ok and api["reachable"] and api["status"] == 200
    else:
        event["passed"] = False
    event["finished_at"] = utc_now()
    return event


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Docker 故障注入验证报告", "", f"- 生成时间：{report['finished_at']}", f"- Compose：`{report['compose_file']}`", f"- 结论：{'通过' if report['passed'] else '未通过'}", "", "| 故障服务 | 停止已观察 | 恢复健康 | 依赖探针 | API 恢复 | 结论 |", "| --- | --- | --- | --- | --- | --- |"]
    for event in report["events"]:
        recovery = event.get("recovery", {})
        api = recovery.get("api", {})
        lines.append(f"| {event['service']} | {event.get('fault', {}).get('observed', False)} | {recovery.get('observed', False)} | {recovery.get('dependency_probe', False)} | {api.get('status', 0) == 200} | {'通过' if event.get('passed') else '未通过'} |")
    lines.extend(["", "## 验证范围", "", "- Redis：停止容器、确认故障、启动并验证 `PING` 与 API 恢复。", "- PostgreSQL：停止容器、确认故障、启动并验证 `pg_isready` 与 API 恢复。", "- Worker：停止容器、确认故障、启动并等待健康检查恢复。", "- 脚本不删除容器、卷或数据，不执行 `down -v`。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Docker Redis/Postgres/Worker 故障与恢复")
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--token", default="")
    parser.add_argument("--services", default=",".join(SERVICES))
    parser.add_argument("--recovery-timeout", type=float, default=120)
    parser.add_argument("--settle", type=float, default=2)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    require_test_environment(args.base_url, args.allow_remote)
    selected = [item.strip().lower() for item in args.services.split(",") if item.strip()]
    if not selected or any(item not in SERVICES for item in selected):
        parser.error(f"services 仅支持：{','.join(SERVICES)}")
    compose_file = args.compose_file.resolve()
    compose(compose_file, "config", "--quiet")
    compose(compose_file, "up", "-d", *SERVICES, timeout=max(args.recovery_timeout, 120))
    ready, last = wait_until(lambda: all(service_ready(compose_file, service) for service in SERVICES), args.recovery_timeout, 1)
    if not ready:
        raise RuntimeError(f"初始服务未全部就绪：{last}")
    report: dict[str, Any] = {"kind": "docker_fault_injection", "started_at": utc_now(), "compose_file": str(compose_file), "services": selected, "events": []}
    try:
        for service in selected:
            try:
                report["events"].append(inject_and_restore(compose_file, service, args.base_url, args.token, args.recovery_timeout, args.settle))
            except Exception as exc:
                report["events"].append({"service": service, "started_at": utc_now(), "finished_at": utc_now(), "passed": False, "error": str(exc)})
    finally:
        for service in selected:
            state = service_state(compose_file, service)
            if state["status"] != "running":
                result = run_process(compose_command(compose_file, "up", "-d", service), compose_file.parent, max(args.recovery_timeout, 120))
                if result.returncode:
                    report.setdefault("cleanup_errors", []).append({"service": service, "error": (result.stderr or result.stdout).strip()})
    report["finished_at"] = utc_now()
    report["passed"] = len(report["events"]) == len(selected) and all(event.get("passed") for event in report["events"]) and not report.get("cleanup_errors")
    stem = f"fault-injection-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    json_path, markdown_path = write_report(args.output_dir, stem, report, render_markdown(report))
    print(json.dumps({"passed": report["passed"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
