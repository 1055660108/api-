from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


LOCAL_TZ = timezone(timedelta(hours=8))


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_task_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise RuntimeError("任务 ID 文件必须是 JSON 数组")
    return {str(item) for item in payload if item}


def collect_success_records(target_date: date, task_ids: set[str] | None) -> tuple[list[dict[str, str]], dict[str, Any]]:
    from app.store import get_meta, list_tasks, load_result

    records: list[dict[str, str]] = []
    skipped: dict[str, list[str]] = {"not_success": [], "outside_date": [], "missing_final_charge": []}
    candidates = [str(item.get("id") or "") for item in list_tasks()]
    if task_ids is not None:
        candidates = sorted(task_ids)
    for task_id in candidates:
        try:
            meta = get_meta(task_id)
        except FileNotFoundError:
            skipped.setdefault("missing_task", []).append(task_id)
            continue
        if str(meta.get("status") or "") != "success":
            skipped["not_success"].append(task_id)
            continue
        finished_at = str(meta.get("finished_at") or meta.get("updated_at") or "")
        finished = parse_time(finished_at)
        if not finished or finished.astimezone(LOCAL_TZ).date() != target_date:
            skipped["outside_date"].append(task_id)
            continue
        result = load_result(task_id)
        account_id = str(result.get("account_id") or "")
        charge_id = str(result.get("account_quota_charge_id") or "")
        if not account_id or not charge_id:
            skipped["missing_final_charge"].append(task_id)
            continue
        records.append({"task_id": task_id, "account_id": account_id, "charge_id": charge_id, "finished_at": finished_at})
    return records, {key: value for key, value in skipped.items() if value}


def main() -> int:
    parser = argparse.ArgumentParser(description="按当日成功任务的最终账号和 charge 补齐已用额度")
    parser.add_argument("--date", default=datetime.now(LOCAL_TZ).date().isoformat())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--task-ids", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["DOLA_DATA_DIR"] = str(args.data_dir.resolve())
        os.environ["DOLA_CONFIG_PATH"] = str((args.data_dir / "config.json").resolve())
    target_date = date.fromisoformat(args.date)
    if args.apply and target_date != datetime.now(LOCAL_TZ).date():
        raise RuntimeError("写入模式只允许对账当日成功任务")
    records, skipped = collect_success_records(target_date, load_task_ids(args.task_ids))
    from app.accounts import reconcile_success_quota_charges

    report = reconcile_success_quota_charges(records, dry_run=not args.apply)
    report.update(date=target_date.isoformat(), selected_success_tasks=len(records), skipped=skipped)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["missing_account_ids"] and not skipped.get("missing_final_charge") else 1


if __name__ == "__main__":
    raise SystemExit(main())
