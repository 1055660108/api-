from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

from scripts.validation_common import compose_command, require_test_environment, run_checked, run_process, utc_now, write_report


def compose(compose_file: Path, *arguments: str, timeout: float = 120) -> str:
    return run_checked(compose_command(compose_file, *arguments), compose_file.parent, timeout)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(compose_file: Path, output_dir: Path, database: str, user: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    postgres_path = output_dir / "postgres.sql"
    app_path = output_dir / "app-data.tar.gz"
    redis_path = output_dir / "redis.rdb"
    dump = compose(compose_file, "exec", "-T", "postgres", "pg_dump", "--clean", "--if-exists", "-U", user, "-d", database, timeout=300)
    postgres_path.write_text(dump, encoding="utf-8")
    compose(compose_file, "run", "--rm", "--user", "0:0", "-v", f"{output_dir.resolve()}:/backup", "api", "python", "-c", "import tarfile; t=tarfile.open('/backup/app-data.tar.gz','w:gz'); t.add('/var/lib/dola-fetch-service',arcname='data'); t.close()", timeout=300)
    compose(compose_file, "exec", "-T", "redis", "redis-cli", "SAVE")
    compose(compose_file, "cp", "redis:/data/dump.rdb", str(redis_path.resolve()), timeout=120)
    artifacts = {}
    for path in (postgres_path, app_path, redis_path):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"备份产物为空：{path}")
        artifacts[path.name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    manifest = {"created_at": utc_now(), "database": database, "artifacts": artifacts}
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def restore_verify(compose_file: Path, backup_dir: Path, user: str) -> dict[str, Any]:
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, metadata in manifest["artifacts"].items():
        path = backup_dir / name
        if not path.is_file() or sha256(path) != metadata["sha256"]:
            raise RuntimeError(f"备份校验失败：{name}")
    with tarfile.open(backup_dir / "app-data.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
        if not members or any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
            raise RuntimeError("应用数据归档不安全或为空")
    compose(compose_file, "run", "--rm", "--user", "0:0", "-v", f"{backup_dir.resolve()}:/backup:ro", "redis", "redis-check-rdb", "/backup/redis.rdb")
    verify_database = f"dola_acceptance_restore_{os.getpid()}"
    compose(compose_file, "exec", "-T", "postgres", "createdb", "-U", user, verify_database)
    try:
        sql = (backup_dir / "postgres.sql").read_text(encoding="utf-8")
        command = compose_command(compose_file, "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", verify_database)
        result = subprocess.run(command, cwd=compose_file.parent, input=sql, text=True, capture_output=True, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        table_count = int(compose(compose_file, "exec", "-T", "postgres", "psql", "-U", user, "-d", verify_database, "-Atc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"))
        if table_count < 1:
            raise RuntimeError("恢复数据库未包含业务表")
    finally:
        run_process(compose_command(compose_file, "exec", "-T", "postgres", "dropdb", "--if-exists", "-U", user, verify_database), compose_file.parent, 120)
    return {"ok": True, "database_tables": table_count, "artifacts": manifest["artifacts"]}


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# 备份恢复验证报告", "", f"- 生成时间：{report['finished_at']}", f"- 结论：{'通过' if report['passed'] else '未通过'}", "", "| 产物 | 字节数 | SHA-256 |", "| --- | ---: | --- |"]
    for name, metadata in report.get("backup", {}).get("artifacts", {}).items():
        lines.append(f"| {name} | {metadata['bytes']} | `{metadata['sha256']}` |")
    lines.extend(["", f"- PostgreSQL 恢复后业务表数：{report.get('restore', {}).get('database_tables', 0)}", "- 应用数据归档已执行路径安全检查。", "- Redis RDB 已通过 redis-check-rdb。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="创建并非破坏性验证 Compose 数据备份")
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    parser.add_argument("--database", default=os.environ.get("POSTGRES_DB", "dola"))
    parser.add_argument("--user", default=os.environ.get("POSTGRES_USER", "dola"))
    args = parser.parse_args()
    require_test_environment()
    compose_file = args.compose_file.resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_dir = args.output_dir.resolve() / f"acceptance-{stamp}"
    report: dict[str, Any] = {"kind": "backup_restore", "started_at": utc_now(), "backup_dir": str(backup_dir)}
    try:
        report["backup"] = create_backup(compose_file, backup_dir, args.database, args.user)
        report["restore"] = restore_verify(compose_file, backup_dir, args.user)
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["error"] = str(exc)
    report["finished_at"] = utc_now()
    json_path, markdown_path = write_report(backup_dir, "verification", report, render_markdown(report))
    print(json.dumps({"passed": report["passed"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
