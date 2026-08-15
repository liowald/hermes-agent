#!/usr/bin/env python3
"""Cheap deterministic Hermes factory probe and reconciler.

Every tick emits one stable JSON snapshot. The cron monitor owns change
detection, so an unchanged healthy or anomalous snapshot invokes no model.
Deployment must seed the initial healthy monitor baseline before enabling the
job; later transitions alone wake the Terra anomaly reviewer.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_factory as factory


STALL_SECONDS = 12 * 60
STATE_PATH = Path.home() / ".hermes" / "factory" / "watchdog-state.json"
GIT_COMMAND_TIMEOUT_SECONDS = 15
MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RELEVANT_PATHS = 20_000
MAX_RELEVANT_FILE_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class SemanticProbe:
    digest: str
    complete: bool
    error: str | None = None


class _ProbeIncomplete(RuntimeError):
    pass


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_PATH.with_suffix(".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


def _git_output(workspace: Path, label: str, argv: list[str]) -> bytes:
    env = os.environ.copy()
    env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", *argv],
            cwd=workspace,
            env=env,
            capture_output=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _ProbeIncomplete(f"git_{label}_unavailable") from exc
    if result.returncode != 0:
        raise _ProbeIncomplete(f"git_{label}_failed")
    if len(result.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise _ProbeIncomplete(f"git_{label}_oversized")
    return result.stdout


def _relevant_git_paths(workspace: Path) -> tuple[bytes, list[bytes]]:
    index = _git_output(workspace, "index", ["ls-files", "--stage", "-z"])
    changed = _git_output(
        workspace,
        "worktree",
        ["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--"],
    )
    untracked = _git_output(
        workspace,
        "untracked",
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    paths = sorted({value for value in (changed + untracked).split(b"\0") if value})
    if len(paths) > MAX_RELEVANT_PATHS:
        raise _ProbeIncomplete("relevant_path_limit")
    for value in paths:
        if value.startswith((b"/", b"\\")) or b".." in value.replace(b"\\", b"/").split(b"/"):
            raise _ProbeIncomplete("unsafe_git_path")
    return index, paths


def _hash_relevant_files(digest, workspace: Path, paths: list[bytes]) -> None:
    consumed = 0
    for raw_path in paths:
        relative = os.fsdecode(raw_path)
        candidate = workspace / relative
        digest.update(b"path\0" + raw_path + b"\0")
        try:
            before = candidate.lstat()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        file_type = stat.S_IFMT(before.st_mode)
        digest.update(f"{file_type:o}:{stat.S_IMODE(before.st_mode):o}\0".encode())
        if stat.S_ISLNK(before.st_mode):
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise _ProbeIncomplete("symlink_read_failed") from exc
            digest.update(b"symlink\0" + os.fsencode(target) + b"\0")
            continue
        if not stat.S_ISREG(before.st_mode):
            raise _ProbeIncomplete("unsupported_file_type")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(candidate, flags)
        except OSError as exc:
            raise _ProbeIncomplete("file_open_failed") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                stat.S_IFMT(opened.st_mode),
            ) != (
                before.st_dev,
                before.st_ino,
                stat.S_IFMT(before.st_mode),
            ):
                raise _ProbeIncomplete("file_changed_during_probe")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > MAX_RELEVANT_FILE_BYTES:
                    raise _ProbeIncomplete("relevant_file_byte_limit")
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _ProbeIncomplete("file_changed_during_probe")
        digest.update(b"\0")


def _semantic_fingerprint(conn, task) -> SemanticProbe:
    run = kb.latest_run(conn, task.id)
    if run is None:
        semantic_events = []
    else:
        semantic_events = [
            (row["id"], row["kind"], row["payload"])
            for row in conn.execute(
                "SELECT id,kind,payload FROM task_events "
                "WHERE task_id=? AND run_id=? "
                "AND kind IN ('progress','review_evidence') ORDER BY id",
                (task.id, run.id),
            )
        ]
    payload = {
        "status": task.status,
        "run": run.id if run else None,
        "outcome": run.outcome if run else None,
        "summary": run.summary if run else None,
        "metadata": run.metadata if run else None,
        "semantic_events": semantic_events,
        "workspace": task.workspace_path,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode())
    if not task.workspace_path:
        return SemanticProbe(digest.hexdigest(), True)
    workspace = Path(task.workspace_path)
    if not workspace.is_dir():
        return SemanticProbe(digest.hexdigest(), False, "workspace_unavailable")
    try:
        head = _git_output(workspace, "head", ["rev-parse", "--verify", "HEAD"])
        index, paths = _relevant_git_paths(workspace)
        digest.update(b"head\0" + head + b"index\0" + index)
        _hash_relevant_files(digest, workspace, paths)
    except _ProbeIncomplete as exc:
        return SemanticProbe(digest.hexdigest(), False, str(exc))
    return SemanticProbe(digest.hexdigest(), True)


def main() -> int:
    now = int(time.time())
    old = _load_state()
    next_state: dict = {}
    anomalies: list[dict] = []

    boards = sorted(
        kb.list_boards(),
        key=lambda board: board.get("slug", "") if isinstance(board, dict) else str(board),
    )
    for board in boards:
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
                    "WHERE delivery_quarantined_at IS NOT NULL "
                    "ORDER BY task_id,platform,chat_id"
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
            for reconciliation in factory.reconcile_all(conn):
                reconcile_error = reconciliation.get("reconcile_error")
                if reconcile_error:
                    anomalies.append({
                        "board": slug,
                        "root_id": reconciliation.get("root_id"),
                        "kind": "reconcile_failed",
                        "detail": str(reconcile_error)[:300],
                    })
            rows = conn.execute(
                "SELECT * FROM factory_workflows ORDER BY created_at,root_id"
            ).fetchall()
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
                        from gateway.status import _pid_exists

                        if not _pid_exists(int(task.worker_pid)):
                            anomalies.append({
                                "board": slug, "root_id": root, "task_id": task_id,
                                "kind": "dead_worker_pid", "detail": str(task.worker_pid),
                            })
                    probe = _semantic_fingerprint(conn, task)
                    key = f"{slug}:{task_id}"
                    prior_value = old.get(key)
                    prior = prior_value if isinstance(prior_value, dict) else {}
                    first_seen = (
                        int(prior.get("first_seen", now))
                        if prior.get("fingerprint") == probe.digest else now
                    )
                    next_state[key] = {
                        "fingerprint": probe.digest,
                        "first_seen": first_seen,
                        "complete": probe.complete,
                    }
                    if not probe.complete:
                        anomalies.append({
                            "board": slug,
                            "root_id": root,
                            "task_id": task_id,
                            "kind": "semantic_probe_incomplete",
                            "detail": probe.error or "unknown",
                        })
                    elif task.status == "running" and now - first_seen >= STALL_SECONDS:
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

    anomalies.sort(
        key=lambda value: (
            str(value.get("board", "")),
            str(value.get("root_id", "")),
            str(value.get("task_id", "")),
            str(value.get("kind", "")),
            str(value.get("detail", "")),
        )
    )
    _save_state(next_state)
    print(json.dumps({
        "contract": "hermes.factory.watch.v1",
        "status": "anomaly" if anomalies else "healthy",
        "anomalies": anomalies,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
