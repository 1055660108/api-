from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts import backup_restore, docker_fault_injection, ladder_concurrency, prepare_https
from scripts.validation_common import compose_command, require_test_environment, run_checked, run_process, utc_now, wait_until, write_report


SERVICES = ("api", "worker", "redis", "postgres")
FAULT_SERVICES = ("api", "redis", "postgres", "worker")
FORBIDDEN_SECRETS = {"", "change-me", "change-me-now", "password", "admin", "test"}
PROBE_TARGETS = (("api", "/health/live", False), ("admin", "/admin", False), ("client", "/client", False), ("authenticated", "/health", True))
MIGRATION_DOCUMENTS = ("runtime.json", "accounts.json", "temp_tokens.json", "users.json", "point_packages.json")


def load_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update({key: value for key, value in os.environ.items() if value})
    return values


def security_gate(base_url: str, environment: dict[str, str], allow_remote: bool) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("一键验收仅允许在 Linux 主机执行")
    if environment.get("DOLA_ACCEPTANCE_ENV", "").lower() != "acceptance":
        raise RuntimeError("安全保护：必须设置 DOLA_ACCEPTANCE_ENV=acceptance")
    require_test_environment(base_url, allow_remote)
    project = environment.get("COMPOSE_PROJECT_NAME", "")
    if not project.startswith("dola-acceptance"):
        raise RuntimeError("COMPOSE_PROJECT_NAME 必须以 dola-acceptance 开头，禁止复用生产卷")
    password = environment.get("POSTGRES_PASSWORD", "")
    admin_password = environment.get("DOLA_ADMIN_PASSWORD", "")
    if password.lower() in FORBIDDEN_SECRETS or len(password) < 16:
        raise RuntimeError("POSTGRES_PASSWORD 必须使用至少 16 位非默认密码")
    if admin_password.lower() in FORBIDDEN_SECRETS or len(admin_password) < 12:
        raise RuntimeError("DOLA_ADMIN_PASSWORD 必须使用至少 12 位非默认密码")
    parsed = urlparse(base_url)
    return {"project": project, "target_host": parsed.hostname, "remote_enabled": allow_remote}


def compose(compose_file: Path, *arguments: str, timeout: float = 120) -> str:
    return run_checked(compose_command(compose_file, *arguments), compose_file.parent, timeout)


def service_snapshot(compose_file: Path) -> dict[str, Any]:
    states = {}
    for service in SERVICES:
        container_id = compose(compose_file, "ps", "-q", service)
        if not container_id:
            states[service] = {"status": "missing", "health": "missing"}
            continue
        state = json.loads(run_checked(["docker", "inspect", "--format", "{{json .State}}", container_id], compose_file.parent))
        states[service] = {"status": state.get("Status", ""), "health": (state.get("Health") or {}).get("Status", "none")}
    return states


def all_services_ready(states: dict[str, Any]) -> bool:
    return all(states.get(service, {}).get("status") == "running" and states[service].get("health") in {"healthy", "none"} for service in SERVICES)


def request_probe(url: str, token: str = "") -> dict[str, Any]:
    headers = {"User-Agent": "dola-linux-acceptance/1.0"}
    if token:
        headers["X-API-Token"] = token
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            body = response.read(4096)
            return {"ok": 200 <= response.status < 400, "status": response.status, "bytes": len(body), "latency_ms": round((time.perf_counter() - started) * 1000, 3)}
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


def continuous_probe(base_url: str, token: str, duration: float, interval: float) -> dict[str, Any]:
    targets = [(name, path, token if authenticated else "") for name, path, authenticated in PROBE_TARGETS]
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            futures = [(name, executor.submit(request_probe, f"{base_url.rstrip('/')}{path}", target_token)) for name, path, target_token in targets]
            sampled_at = utc_now()
            samples.extend({"target": name, "sampled_at": sampled_at, **future.result()} for name, future in futures)
        time.sleep(interval)
    counts = {name: {"total": 0, "failed": 0} for name, _, _ in targets}
    for sample in samples:
        counts[sample["target"]]["total"] += 1
        counts[sample["target"]]["failed"] += 0 if sample["ok"] else 1
    return {"passed": bool(samples) and all(item["total"] > 0 and item["failed"] == 0 for item in counts.values()), "counts": counts, "samples": samples}


class ProbeMonitor:
    def __init__(self, base_url: str, token: str, interval: float):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self.stage = "初始化"
        self.expected_outage = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="acceptance-probe", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def set_stage(self, stage: str, expected_outage: bool = False) -> None:
        with self._lock:
            self.stage = stage
            self.expected_outage = expected_outage

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                stage = self.stage
                expected_outage = self.expected_outage
            sampled_at = utc_now()
            with ThreadPoolExecutor(max_workers=len(PROBE_TARGETS)) as executor:
                futures = [
                    (name, executor.submit(request_probe, f"{self.base_url}{path}", self.token if authenticated else ""))
                    for name, path, authenticated in PROBE_TARGETS
                ]
                batch = [{"target": name, "sampled_at": sampled_at, "stage": stage, "expected_outage": expected_outage, **future.result()} for name, future in futures]
            with self._lock:
                self.samples.extend(batch)
            self._stop.wait(self.interval)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=10)
        counts = {name: {"total": 0, "failed": 0, "expected_failed": 0} for name, _, _ in PROBE_TARGETS}
        stage_counts: dict[str, dict[str, int]] = {}
        with self._lock:
            samples = list(self.samples)
        for sample in samples:
            target = counts[sample["target"]]
            target["total"] += 1
            stage = stage_counts.setdefault(sample["stage"], {"total": 0, "failed": 0, "expected_failed": 0})
            stage["total"] += 1
            if not sample["ok"]:
                key = "expected_failed" if sample["expected_outage"] else "failed"
                target[key] += 1
                stage[key] += 1
        passed = bool(samples) and all(item["total"] > 0 and item["failed"] == 0 for item in counts.values())
        return {"passed": passed, "counts": counts, "stages": stage_counts, "samples": samples}


def api_token(compose_file: Path) -> str:
    code = "from app.config import load_settings; print(load_settings().api_token)"
    token = compose(compose_file, "exec", "-T", "api", "python", "-c", code).strip()
    if len(token) < 20:
        raise RuntimeError("未能读取验收环境 API Token")
    return token


def set_workers(base_url: str, token: str, workers: int) -> dict[str, Any]:
    payload = json.dumps({"browser_workers": workers}).encode("utf-8")
    request = Request(f"{base_url.rstrip('/')}/config/workers", data=payload, method="POST", headers={"Content-Type": "application/json", "X-API-Token": token})
    with urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if response.status != 200 or result.get("browser_workers") != workers:
        raise RuntimeError("30 并发配置未生效")
    return result


def json_snapshot_summary(root: Path) -> dict[str, Any]:
    files = sorted(path for path in root.rglob("*") if path.is_file() and ".json-backup" not in path.relative_to(root).parts)
    tasks = [path for path in (root / "tasks").iterdir() if path.is_dir() and (path / "meta.json").is_file()] if (root / "tasks").is_dir() else []
    documents = [name for name in MIGRATION_DOCUMENTS if (root / name).is_file()]
    digest = sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        if path.suffix.lower() == ".json":
            content = json.dumps(json.loads(content.decode("utf-8-sig")), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        total_bytes += len(content)
    return {"tasks": len(tasks), "documents": len(documents), "files": len(files), "bytes": total_bytes, "sha256": digest.hexdigest()}


def migration_roundtrip(compose_file: Path, environment: dict[str, str]) -> dict[str, Any]:
    user = environment.get("POSTGRES_USER", "dola")
    password = environment["POSTGRES_PASSWORD"]
    database = f"dola_acceptance_migration_{os.getpid()}"
    compose(compose_file, "exec", "-T", "postgres", "createdb", "-U", user, database)
    try:
        with tempfile.TemporaryDirectory(prefix="dola-migration-") as directory:
            validation_dir = Path(directory)
            snapshot_dir = validation_dir / "current-json-readonly"
            data_dir = validation_dir / "migration-working"
            copy_code = "import os,shutil;from pathlib import Path;source=Path(os.environ['DOLA_DATA_DIR']);target=Path('/validation/current-json-readonly');target.mkdir(parents=True,exist_ok=True);names=('config.json','runtime.json','accounts.json','temp_tokens.json','users.json','point_packages.json');[shutil.copy2(source/name,target/name) for name in names if (source/name).is_file()];shutil.copytree(source/'tasks',target/'tasks') if (source/'tasks').is_dir() else None"
            compose(compose_file, "run", "--rm", "--user", "0:0", "-v", f"{validation_dir.resolve()}:/validation", "api", "python", "-c", copy_code, timeout=180)
            source_summary = json_snapshot_summary(snapshot_dir)
            if source_summary["files"] == 0:
                raise RuntimeError("当前 JSON 只读副本为空，拒绝执行迁移验收")
            shutil.copytree(snapshot_dir, data_dir)
            for path in sorted(snapshot_dir.rglob("*"), reverse=True):
                path.chmod(0o500 if path.is_dir() else 0o400)
            snapshot_dir.chmod(0o500)
            database_url = f"postgresql://{user}:{password}@postgres:5432/{database}"
            base = ("run", "--rm", "--user", "0:0", "-v", f"{data_dir.resolve()}:/validation", "-e", "DOLA_DATA_DIR=/validation", "-e", "DOLA_CONFIG_PATH=/validation/config.json", "-e", f"DOLA_DATABASE_URL={database_url}", "api", "python", "scripts/storage_migrate.py")
            to_postgres = json.loads(compose(compose_file, *base, "to-postgres", timeout=180))
            to_json = json.loads(compose(compose_file, *base, "to-json", timeout=180))
            restored_summary = json_snapshot_summary(data_dir)
            migrated_counts = {"tasks": to_postgres.get("tasks"), "documents": to_postgres.get("documents")}
            expected_counts = {"tasks": source_summary["tasks"], "documents": source_summary["documents"]}
            rollback_counts = {"tasks": to_json.get("tasks"), "documents": to_json.get("documents")}
            passed = to_postgres.get("ok") and to_json.get("ok") and migrated_counts == expected_counts and rollback_counts == expected_counts and restored_summary == source_summary
            if not passed:
                raise RuntimeError(f"迁移数量摘要或回滚内容不一致：source={source_summary}, migrated={migrated_counts}, rollback={rollback_counts}, restored={restored_summary}")
            return {"passed": True, "source_readonly": True, "source_summary": source_summary, "migrated_summary": migrated_counts, "rollback_summary": rollback_counts, "restored_summary": restored_summary}
    finally:
        run_process(compose_command(compose_file, "exec", "-T", "postgres", "dropdb", "--if-exists", "-U", user, database), compose_file.parent, 120)


def post_form(url: str, token: str, fields: dict[str, str]) -> dict[str, Any]:
    boundary = f"dola-acceptance-{os.getpid()}-{time.time_ns()}"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")
    request = Request(url, data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-API-Token": token})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str, token: str) -> dict[str, Any]:
    with urlopen(Request(url, headers={"X-API-Token": token}), timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_real_dola_stages(value: str) -> list[int]:
    if not value.strip():
        return []
    stages = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not stages or any(stage not in {1, 3, 5} for stage in stages) or len(stages) != len(set(stages)):
        raise ValueError("真实 Dola 测试仅支持不重复的 1,3,5 分档")
    return stages


def run_real_dola_stage(base_url: str, token: str, concurrency: int, prompt: str, timeout: float, poll_interval: float) -> dict[str, Any]:
    started_at = utc_now()
    submit_url = f"{base_url.rstrip('/')}/tasks"
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(post_form, submit_url, token, {"prompt": f"{prompt} #{index + 1}", "ratio": "9:16", "platform": "dola"}) for index in range(concurrency)]
        submissions = [future.result() for future in futures]
    task_ids = [str(item.get("id") or "") for item in submissions]
    if any(len(task_id) != 32 for task_id in task_ids):
        return {"passed": False, "concurrency": concurrency, "started_at": started_at, "finished_at": utc_now(), "submissions": submissions, "error": "真实 Dola 任务提交未返回有效任务 ID"}
    deadline = time.monotonic() + timeout
    observations: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        listing = get_json(f"{base_url.rstrip('/')}/tasks?{urlencode({'page': 1, 'page_size': 100})}", token)
        listed = {str(item.get("id")): item for item in listing.get("tasks", [])}
        for task_id in task_ids:
            detail = get_json(f"{base_url.rstrip('/')}/tasks/{task_id}", token)
            observations[task_id] = {"task": listed.get(task_id, {}), "result": detail}
        failed = [task_id for task_id, item in observations.items() if item["task"].get("status") in {"failed", "canceled"}]
        if failed:
            return {"passed": False, "concurrency": concurrency, "started_at": started_at, "finished_at": utc_now(), "task_ids": task_ids, "observations": observations, "error": f"真实 Dola 任务失败：{','.join(failed)}"}
        if len(observations) == concurrency and all(item["result"].get("code") == "2" and item["result"].get("url") for item in observations.values()):
            return {"passed": True, "concurrency": concurrency, "started_at": started_at, "finished_at": utc_now(), "task_ids": task_ids, "observations": observations}
        time.sleep(poll_interval)
    return {"passed": False, "concurrency": concurrency, "started_at": started_at, "finished_at": utc_now(), "task_ids": task_ids, "observations": observations, "error": f"真实 Dola 任务在 {timeout:g} 秒内未全部成功"}


def prepare_https_config(args, environment: dict[str, str]) -> dict[str, Any]:
    domain = args.domain or environment.get("DOLA_HTTPS_DOMAIN", "")
    certificate = Path(args.certificate or environment.get("DOLA_TLS_CERTIFICATE", ""))
    private_key = Path(args.private_key or environment.get("DOLA_TLS_PRIVATE_KEY", ""))
    raw_cidrs = args.allow_cidr or [item for item in environment.get("DOLA_IP_ALLOWLIST", "").split(",") if item]
    normalized = prepare_https.validate_cidrs(raw_cidrs)
    validated_domain = prepare_https.validate_domain(domain)
    validated_certificate = prepare_https.validate_certificate(certificate, "BEGIN CERTIFICATE")
    validated_private_key = prepare_https.validate_certificate(private_key, "PRIVATE KEY")
    output = args.output_dir.resolve() / "nginx-dola-https.conf"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(prepare_https.render_nginx(validated_domain, validated_certificate, validated_private_key, normalized, "http://127.0.0.1:8088"), encoding="utf-8")
    output.chmod(0o640)
    return {"passed": True, "domain": validated_domain, "allow_cidrs": normalized, "output": str(output)}


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Linux 一键验收报告", "", f"- 开始时间：{report['started_at']}", f"- 完成时间：{report['finished_at']}", f"- 总结论：{'通过' if report['passed'] else '未通过'}", "", "| 验收项 | 结论 |", "| --- | --- |"]
    for name, stage in report["stages"].items():
        lines.append(f"| {name} | {'通过' if stage.get('passed') else '未通过'} |")
    lines.extend(["", "## 安全边界", "", "- 仅允许 Linux、显式验收环境和独立 dola-acceptance Compose 项目。", "- 拒绝默认密码、生产卷复用和未显式授权的远程目标。", "- 故障恢复不删除容器和数据卷，备份恢复使用临时数据库验证。", "- HTTPS 配置拒绝全网开放白名单，仅反代本机 API。", ""])
    if report.get("error"):
        lines.extend(["## 失败原因", "", report["error"], ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Linux 四服务一键验收编排")
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--probe-duration", type=float, default=30)
    parser.add_argument("--probe-interval", type=float, default=2)
    parser.add_argument("--load-duration", type=float, default=15)
    parser.add_argument("--recovery-timeout", type=float, default=120)
    parser.add_argument("--domain", default="")
    parser.add_argument("--certificate", default="")
    parser.add_argument("--private-key", default="")
    parser.add_argument("--allow-cidr", action="append")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--real-dola-stages", default="")
    parser.add_argument("--real-dola-prompt", default="Linux 验收测试：一个人在公园慢跑，镜头平稳")
    parser.add_argument("--real-dola-timeout", type=float, default=900)
    parser.add_argument("--real-dola-poll-interval", type=float, default=10)
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    compose_file = args.compose_file.resolve()
    environment = load_environment(args.env_file.resolve())
    os.environ.update(environment)
    report: dict[str, Any] = {"kind": "linux_acceptance", "started_at": utc_now(), "compose_file": str(compose_file), "stages": {}}
    monitor: ProbeMonitor | None = None
    try:
        real_dola_stages = parse_real_dola_stages(args.real_dola_stages)
        report["stages"]["安全门禁"] = {"passed": True, **security_gate(args.base_url, environment, args.allow_remote)}
        compose(compose_file, "config", "--quiet")
        compose(compose_file, "up", "-d", "--build", *SERVICES, timeout=1200)
        ready, states = wait_until(lambda: (lambda value: value if all_services_ready(value) else False)(service_snapshot(compose_file)), args.recovery_timeout, 2)
        report["stages"]["四服务启动"] = {"passed": ready, "services": states}
        if not ready:
            raise RuntimeError(f"四服务未全部健康：{states}")
        token = api_token(compose_file)
        report["stages"]["30并发配置"] = {"passed": True, **set_workers(args.base_url, token, 30)}
        baseline_probe = continuous_probe(args.base_url, token, args.probe_duration, args.probe_interval)
        report["stages"]["基线网页与API探活"] = baseline_probe
        if not baseline_probe["passed"]:
            raise RuntimeError("网页或 API 连续探活失败")
        monitor = ProbeMonitor(args.base_url, token, args.probe_interval)
        monitor.start()
        monitor.set_stage("30并发压测")
        load_summary, resources = ladder_concurrency.run_stage(f"{args.base_url.rstrip('/')}/health/live", "", 30, args.load_duration, 5, 20260718, compose_file, 1)
        report["stages"]["30并发资源"] = {"passed": load_summary["errors"] == 0 and load_summary["requests"] > 0 and bool(load_summary["resource_peaks"]), "summary": load_summary, "resource_samples": resources}
        if not report["stages"]["30并发资源"]["passed"]:
            raise RuntimeError("30 并发或资源峰值采集失败")
        monitor.set_stage("迁移回滚")
        report["stages"]["迁移回滚"] = migration_roundtrip(compose_file, environment)
        fault_events = []
        for service in FAULT_SERVICES:
            monitor.set_stage(f"故障恢复:{service}", expected_outage=service == "api")
            fault_events.append(docker_fault_injection.inject_and_restore(compose_file, service, args.base_url, token, args.recovery_timeout, 2))
        report["stages"]["故障恢复"] = {"passed": all(event.get("passed") for event in fault_events), "events": fault_events}
        if not report["stages"]["故障恢复"]["passed"]:
            raise RuntimeError("故障恢复验证失败")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        backup_dir = args.backup_dir.resolve() / f"acceptance-{stamp}"
        monitor.set_stage("备份恢复")
        backup = backup_restore.create_backup(compose_file, backup_dir, environment.get("POSTGRES_DB", "dola"), environment.get("POSTGRES_USER", "dola"))
        restore = backup_restore.restore_verify(compose_file, backup_dir, environment.get("POSTGRES_USER", "dola"))
        report["stages"]["备份恢复"] = {"passed": True, "backup_dir": str(backup_dir), "backup": backup, "restore": restore}
        if real_dola_stages:
            real_results = []
            for concurrency in real_dola_stages:
                monitor.set_stage(f"真实Dola:{concurrency}")
                try:
                    result = run_real_dola_stage(args.base_url, token, concurrency, args.real_dola_prompt, args.real_dola_timeout, args.real_dola_poll_interval)
                except Exception as exc:
                    result = {"passed": False, "concurrency": concurrency, "started_at": utc_now(), "finished_at": utc_now(), "error": str(exc)}
                real_results.append(result)
                if not result["passed"]:
                    break
            report["stages"]["真实Dola分档"] = {"passed": len(real_results) == len(real_dola_stages) and all(item["passed"] for item in real_results), "requested_stages": real_dola_stages, "results": real_results}
            if not report["stages"]["真实Dola分档"]["passed"]:
                raise RuntimeError(real_results[-1].get("error", "真实 Dola 分档测试失败"))
        report["stages"]["IP白名单HTTPS准备"] = prepare_https_config(args, environment)
        final_states = service_snapshot(compose_file)
        report["stages"]["最终健康"] = {"passed": all_services_ready(final_states), "services": final_states}
        report["passed"] = all(stage.get("passed") for stage in report["stages"].values())
    except Exception as exc:
        report["passed"] = False
        report["error"] = str(exc)
        try:
            report["final_services"] = service_snapshot(compose_file)
        except Exception as state_error:
            report["final_services_error"] = str(state_error)
    finally:
        if monitor is not None:
            probe = monitor.stop()
            report["stages"]["全程并行探活"] = probe
            if not probe["passed"]:
                report["passed"] = False
                report.setdefault("error", "故障、迁移、备份或 30 并发期间 admin/client/API 探活失败")
    report["finished_at"] = utc_now()
    stem = f"linux-acceptance-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    json_path, markdown_path = write_report(args.output_dir.resolve(), stem, report, render_markdown(report))
    print(json.dumps({"passed": report["passed"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
