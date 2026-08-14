#!/usr/bin/env python3
"""Cheap deterministic Hermes factory probe and reconciler.

Healthy ticks print nothing.  Stable actionable anomalies are emitted as JSON
for a monitor-mode Terra job; the model is never invoked on an unchanged or
healthy snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_factory as factory


STALL_SECONDS = 12 * 60
STATE_PATH = Path.home() / ".hermes" / "factory" / "watchdog-state.json"


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_PATH)


def _semantic_fingerprint(conn, task) -> str:
    run = kb.latest_run(conn, task.id)
    payload = {
        "status": task.status,
        "run": run.id if run else None,
        "outcome": run.outcome if run else None,
        "summary": run.summary if run else None,
        "metadata": run.metadata if run else None,
        "workspace": task.workspace_path,
    }
    if task.workspace_path and Path(task.workspace_path).is_dir():
        import subprocess

        for key, argv in (
            ("head", ["git", "rev-parse", "HEAD"]),
            ("status", ["git", "status", "--porcelain=v1"]),
        ):
            try:
                payload[key] = subprocess.run(
                    argv, cwd=task.workspace_path, text=True, capture_output=True,
                    timeout=15, check=True,
                ).stdout
            except Exception:
                payload[key] = "unavailable"
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def main() -> int:
    now = int(time.time())
    old = _load_state()
    next_state: dict = {}
    anomalies: list[dict] = []

    for board in kb.list_boards():
        slug = board.get("slug") if isinstance(board, dict) else str(board)
        try:
            conn = kb.connect(board=slug)
        except Exception as exc:
            anomalies.append({"board": slug, "kind": "db_unavailable", "detail": str(exc)[:300]})
            continue
        try:
            notify_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(kanban_notify_subs)"
                )
            }
            if "delivery_quarantined_at" in notify_columns:
                for sub in conn.execute(
                    "SELECT task_id,platform,chat_id,delivery_failures,"
                    "delivery_last_error,delivery_quarantined_at "
                    "FROM kanban_notify_subs "
                    "WHERE delivery_quarantined_at IS NOT NULL"
                ):
                    anomalies.append({
                        "board": slug,
                        "task_id": sub["task_id"],
                        "kind": "notification_quarantined",
                        "detail": (
                            f"{sub['platform']}/{sub['chat_id']} after "
                            f"{sub['delivery_failures']} failures: "
                            f"{sub['delivery_last_error'] or 'unknown'}"
                        )[:300],
                    })
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='factory_workflows'"
            ).fetchone()
            if table is None:
                continue
            try:
                factory.reconcile_all(conn)
            except Exception as exc:
                anomalies.append({"board": slug, "kind": "reconcile_failed", "detail": str(exc)[:300]})
            rows = conn.execute("SELECT * FROM factory_workflows ORDER BY created_at").fetchall()
            for row in rows:
                root = row["root_id"]
                if int(row["contract_version"]) > factory.FACTORY_VERSION:
                    anomalies.append({
                        "board": slug, "root_id": root, "kind": "runtime_schema_mismatch",
                        "detail": f"card v{row['contract_version']} runtime v{factory.FACTORY_VERSION}",
                    })
                if row["state"] == "blocked":
                    anomalies.append({
                        "board": slug, "root_id": root, "kind": "factory_blocked",
                        "detail": row["last_error"] or "unknown",
                    })
                active_ids = [
                    row[name] for name in (
                        "implement_task_id", "reviewer_a_task_id", "reviewer_b_task_id",
                        "fixer_task_id", "delivery_task_id",
                    ) if row[name]
                ]
                for task_id in active_ids:
                    task = kb.get_task(conn, task_id)
                    if task is None or task.status not in {"ready", "running", "review"}:
                        continue
                    running_count = conn.execute(
                        "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND status='running'",
                        (task_id,),
                    ).fetchone()[0]
                    if running_count > 1:
                        anomalies.append({
                            "board": slug, "root_id": root, "task_id": task_id,
                            "kind": "duplicate_writers", "detail": str(running_count),
                        })
                    if task.status == "running" and task.worker_pid:
                        try:
                            os.kill(int(task.worker_pid), 0)
                        except OSError:
                            anomalies.append({
                                "board": slug, "root_id": root, "task_id": task_id,
                                "kind": "dead_worker_pid", "detail": str(task.worker_pid),
                            })
                    fingerprint = _semantic_fingerprint(conn, task)
                    key = f"{slug}:{task_id}"
                    prior_value = old.get(key)
                    prior = prior_value if isinstance(prior_value, dict) else {}
                    first_seen = (
                        int(prior.get("first_seen", now))
                        if prior.get("fingerprint") == fingerprint else now
                    )
                    next_state[key] = {"fingerprint": fingerprint, "first_seen": first_seen}
                    if task.status == "running" and now - first_seen >= STALL_SECONDS:
                        anomalies.append({
                            "board": slug, "root_id": root, "task_id": task_id,
                            "kind": "no_semantic_progress",
                            "detail": f"unchanged for at least {STALL_SECONDS}s",
                        })
                root_task = kb.get_task(conn, root)
                if root_task and root_task.status == "done" and row["state"] != "done":
                    anomalies.append({
                        "board": slug, "root_id": root, "kind": "false_completion",
                        "detail": f"root done while factory state is {row['state']}",
                    })
        finally:
            conn.close()

    _save_state(next_state)
    print(json.dumps({
        "contract": "hermes.factory.watch.v1",
        "status": "anomaly" if anomalies else "healthy",
        "anomalies": anomalies,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
