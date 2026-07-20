from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DOCUMENT_FILES = {
    "runtime": "runtime.json",
    "accounts": "accounts.json",
    "temp_tokens": "temp_tokens.json",
    "users": "users.json",
    "point_packages": "point_packages.json",
    "feedback": "feedback.json",
}


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return loaded


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.migration.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _assert_stopped(marker: Path, force: bool) -> None:
    if force:
        return
    if marker.exists():
        raise RuntimeError(f"检测到服务运行标记 {marker}；请停机后重试，或确认无进程后使用 --force")


def migrate_to_postgres(data_dir: Path, backup_dir: Path) -> dict[str, int]:
    from app import postgres

    if not postgres.enabled():
        raise RuntimeError("DOLA_DATABASE_URL is required")
    if backup_dir.exists():
        raise RuntimeError(f"backup directory already exists: {backup_dir}")
    backup_dir.mkdir(parents=True)
    postgres.ensure_schema()
    if postgres.list_task_ids() or any(postgres.read_document(name) for name in DOCUMENT_FILES):
        raise RuntimeError("PostgreSQL target is not empty")
    tasks_dir = data_dir / "tasks"
    task_count = 0
    document_count = 0
    try:
        for name, filename in DOCUMENT_FILES.items():
            source = data_dir / filename
            if source.exists():
                payload = _load_json(source, {})
                postgres.write_document(name, payload)
                shutil.copy2(source, backup_dir / filename)
                document_count += 1
        if tasks_dir.exists():
            for task_dir in sorted(tasks_dir.iterdir()):
                meta_path = task_dir / "meta.json"
                if not task_dir.is_dir() or not meta_path.exists():
                    continue
                meta = _load_json(meta_path, {})
                task_id = str(meta.get("id") or task_dir.name)
                if not postgres.create_task(task_id, meta):
                    raise RuntimeError(f"duplicate task: {task_id}")
                result_path = task_dir / "result.json"
                if result_path.exists():
                    postgres.write_task_part(task_id, "result", _load_json(result_path, {}))
                target = backup_dir / "tasks" / task_dir.name
                shutil.copytree(task_dir, target)
                task_count += 1
        manifest = {"direction": "json-to-postgres", "tasks": task_count, "documents": document_count}
        _write_json(backup_dir / "manifest.json", manifest)
        return {"tasks": task_count, "documents": document_count}
    except Exception:
        postgres.clear_all()
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise


def rollback_to_json(data_dir: Path, backup_dir: Path) -> dict[str, int]:
    from app import postgres

    if not postgres.enabled():
        raise RuntimeError("DOLA_DATABASE_URL is required")
    if not backup_dir.exists():
        raise RuntimeError(f"backup directory does not exist: {backup_dir}")
    staging = data_dir.with_name(f"{data_dir.name}.rollback-staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    task_ids = postgres.list_task_ids()
    document_count = 0
    for name, filename in DOCUMENT_FILES.items():
        payload = postgres.read_document(name)
        if payload:
            _write_json(staging / filename, payload)
            document_count += 1
    for task_id in task_ids:
        task_target = staging / "tasks" / task_id
        task_target.mkdir(parents=True)
        _write_json(task_target / "meta.json", postgres.read_task_part(task_id, "meta"))
        result = postgres.read_task_part(task_id, "result", {})
        if result:
            _write_json(task_target / "result.json", result)
        backup_images = backup_dir / "tasks" / task_id / "images"
        if backup_images.exists():
            shutil.copytree(backup_images, task_target / "images")
        else:
            (task_target / "images").mkdir()
    for child in staging.iterdir():
        target = data_dir / child.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        child.replace(target)
    staging.rmdir()
    return {"tasks": len(task_ids), "documents": document_count}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("direction", choices=("to-postgres", "to-json"))
    parser.add_argument("--data-dir", default=os.environ.get("DOLA_DATA_DIR", "/var/lib/dola-fetch-service"))
    parser.add_argument("--backup-dir")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else data_dir / ".json-backup"
    _assert_stopped(data_dir / ".service-running", args.force)
    _assert_stopped(data_dir / ".worker-health.json", args.force)
    result = migrate_to_postgres(data_dir, backup_dir) if args.direction == "to-postgres" else rollback_to_json(data_dir, backup_dir)
    print(json.dumps({"ok": True, "direction": args.direction, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
