from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.validation_common import compose_command, parse_pair, percentile, require_test_environment, run_checked, utc_now, write_report


def parse_stages(value: str) -> list[int]:
    stages = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not stages or any(stage < 1 or stage > 1000 for stage in stages):
        raise argparse.ArgumentTypeError("阶梯并发必须是 1-1000 的逗号分隔整数")
    return stages


def request_once(url: str, token: str, timeout: float, request_id: str) -> dict[str, Any]:
    headers = {"User-Agent": "dola-test-ladder/1.0", "X-Dola-Test-Request": request_id}
    if token:
        headers["X-API-Token"] = token
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            response.read()
            status = response.status
            error = ""
    except HTTPError as exc:
        exc.read()
        status = exc.code
        error = str(exc)
    except (URLError, TimeoutError, OSError) as exc:
        status = 0
        error = str(exc)
    return {"status": status, "latency_ms": round((time.perf_counter() - started) * 1000, 3), "error": error}


def collect_docker_stats(compose_file: Path) -> list[dict[str, Any]]:
    output = run_checked(compose_command(compose_file, "ps", "-q"), compose_file.parent)
    container_ids = [item for item in output.splitlines() if item.strip()]
    if not container_ids:
        return []
    stats_output = run_checked(["docker", "stats", "--no-stream", "--format", "{{json .}}", *container_ids], compose_file.parent)
    samples = []
    for line in stats_output.splitlines():
        item = json.loads(line)
        memory_used, memory_limit = parse_pair(item.get("MemUsage", ""))
        network_in, network_out = parse_pair(item.get("NetIO", ""))
        block_read, block_write = parse_pair(item.get("BlockIO", ""))
        samples.append({
            "name": item.get("Name", ""),
            "cpu_percent": float(str(item.get("CPUPerc", "0")).rstrip("%") or 0),
            "memory_used_bytes": memory_used,
            "memory_limit_bytes": memory_limit,
            "memory_percent": float(str(item.get("MemPerc", "0")).rstrip("%") or 0),
            "network_in_bytes": network_in,
            "network_out_bytes": network_out,
            "block_read_bytes": block_read,
            "block_write_bytes": block_write,
            "pids": int(item.get("PIDs") or 0),
        })
    return samples


def summarize_stage(concurrency: int, duration: float, results: list[dict[str, Any]], resources: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(item["latency_ms"]) for item in results]
    successes = sum(200 <= int(item["status"]) < 400 for item in results)
    status_counts: dict[str, int] = {}
    for item in results:
        key = str(item["status"])
        status_counts[key] = status_counts.get(key, 0) + 1
    peaks: dict[str, dict[str, Any]] = {}
    for sample in resources:
        name = str(sample["name"])
        peak = peaks.setdefault(name, {"cpu_percent": 0.0, "memory_used_bytes": 0, "memory_percent": 0.0, "pids": 0})
        for key in peak:
            peak[key] = max(peak[key], sample[key])
    return {
        "concurrency": concurrency,
        "duration_seconds": round(duration, 3),
        "requests": len(results),
        "successes": successes,
        "errors": len(results) - successes,
        "requests_per_second": round(len(results) / duration, 3) if duration else 0,
        "latency_ms": {"min": min(latencies, default=0), "p50": percentile(latencies, 0.50), "p95": percentile(latencies, 0.95), "p99": percentile(latencies, 0.99), "max": max(latencies, default=0)},
        "status_counts": status_counts,
        "resource_peaks": peaks,
    }


def run_stage(url: str, token: str, concurrency: int, duration: float, timeout: float, seed: int, compose_file: Path | None, sample_interval: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stop = threading.Event()
    results: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    lock = threading.Lock()
    deadline = time.monotonic() + duration

    def worker(worker_index: int) -> None:
        sequence = 0
        while time.monotonic() < deadline:
            source = f"{seed}:{concurrency}:{worker_index}:{sequence}".encode("utf-8")
            request_id = hashlib.sha256(source).hexdigest()[:24]
            outcome = request_once(url, token, timeout, request_id)
            with lock:
                results.append(outcome)
            sequence += 1

    def monitor() -> None:
        while not stop.is_set():
            sampled_at = utc_now()
            try:
                samples = collect_docker_stats(compose_file) if compose_file else []
                for sample in samples:
                    resources.append({"sampled_at": sampled_at, **sample})
            except Exception as exc:
                resources.append({"sampled_at": sampled_at, "name": "collector", "error": str(exc)})
            stop.wait(sample_interval)

    started = time.monotonic()
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker, index) for index in range(concurrency)]
        for future in futures:
            future.result()
    stop.set()
    monitor_thread.join(timeout=sample_interval + 2)
    elapsed = time.monotonic() - started
    valid_resources = [item for item in resources if "error" not in item]
    return summarize_stage(concurrency, elapsed, results, valid_resources), resources


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# 阶梯并发与资源峰值报告", "", f"- 生成时间：{report['finished_at']}", f"- 目标：`{report['target']}`", f"- 随机种子：`{report['seed']}`", f"- 结论：{'通过' if report['passed'] else '未通过'}", "", "| 并发 | 请求数 | RPS | 成功 | 错误 | P50(ms) | P95(ms) | P99(ms) |", "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for stage in report["stages"]:
        latency = stage["latency_ms"]
        lines.append(f"| {stage['concurrency']} | {stage['requests']} | {stage['requests_per_second']} | {stage['successes']} | {stage['errors']} | {latency['p50']} | {latency['p95']} | {latency['p99']} |")
    lines.extend(["", "## 资源峰值", ""])
    for stage in report["stages"]:
        lines.append(f"### 并发 {stage['concurrency']}")
        lines.append("")
        lines.append("| 容器 | CPU(%) | 内存(MiB) | 内存(%) | PIDs |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for name, peak in stage["resource_peaks"].items():
            lines.append(f"| {name} | {peak['cpu_percent']:.2f} | {peak['memory_used_bytes'] / 1024 / 1024:.2f} | {peak['memory_percent']:.2f} | {peak['pids']} |")
        if not stage["resource_peaks"]:
            lines.append("| 未启用 Docker 采集 | 0 | 0 | 0 | 0 |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="执行可复现阶梯并发并采集 Docker 资源峰值")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--path", default="/health/live")
    parser.add_argument("--token", default="")
    parser.add_argument("--stages", type=parse_stages, default=parse_stages("1,5,10,20"))
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--cooldown", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--seed", type=int, default=20260718)
    parser.add_argument("--compose-file", type=Path)
    parser.add_argument("--sample-interval", type=float, default=1)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    target = f"{args.base_url.rstrip('/')}/{args.path.lstrip('/')}"
    require_test_environment(args.base_url, args.allow_remote)
    if args.duration <= 0 or args.cooldown < 0 or args.timeout <= 0 or args.sample_interval <= 0:
        parser.error("duration、timeout、sample-interval 必须为正数，cooldown 不得为负数")
    compose_file = args.compose_file.resolve() if args.compose_file else None
    started_at = utc_now()
    stages = []
    raw_resources: dict[str, list[dict[str, Any]]] = {}
    for index, concurrency in enumerate(args.stages):
        summary, samples = run_stage(target, args.token, concurrency, args.duration, args.timeout, args.seed, compose_file, args.sample_interval)
        stages.append(summary)
        raw_resources[str(concurrency)] = samples
        if index + 1 < len(args.stages):
            time.sleep(args.cooldown)
    report = {"kind": "ladder_concurrency", "started_at": started_at, "finished_at": utc_now(), "target": target, "seed": args.seed, "stages": stages, "resource_samples": raw_resources, "passed": all(stage["errors"] == 0 and stage["requests"] > 0 for stage in stages)}
    stem = f"ladder-{datetime_stamp()}"
    json_path, markdown_path = write_report(args.output_dir, stem, report, render_markdown(report))
    print(json.dumps({"passed": report["passed"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def datetime_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


if __name__ == "__main__":
    raise SystemExit(main())
