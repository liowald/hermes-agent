from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Watcher Test")
    _git(repo, "config", "user.email", "watcher@example.invalid")
    source = repo / "source.txt"
    source.write_text("base\n")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-qm", "base")
    return repo


def _probe(conn, watchdog, repo, task_id="probe-task"):
    task = SimpleNamespace(
        id=task_id,
        status="running",
        workspace_path=str(repo) if repo else None,
    )
    return watchdog._semantic_fingerprint(conn, task)


def test_semantic_probe_tracks_same_path_content_stage_and_commit(tmp_path):
    from hermes_cli import kanban_db as kb
    from scripts import hermes_factory_watchdog as watchdog

    repo = _repo(tmp_path)
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        source = repo / "source.txt"
        source.write_text("first\n")
        first = _probe(conn, watchdog, repo)
        source.write_text("later\n")
        second = _probe(conn, watchdog, repo)
        assert first.complete and second.complete
        assert first.digest != second.digest
        assert _git(repo, "status", "--porcelain=v1") == "M source.txt"

        os.utime(source, None)
        touched = _probe(conn, watchdog, repo)
        assert touched.digest == second.digest

        _git(repo, "add", "source.txt")
        staged = _probe(conn, watchdog, repo)
        assert staged.digest != touched.digest
        _git(repo, "commit", "-qm", "later")
        committed = _probe(conn, watchdog, repo)
        assert committed.digest != staged.digest
    finally:
        conn.close()


def test_semantic_probe_tracks_untracked_content_and_symlink_target(tmp_path):
    from hermes_cli import kanban_db as kb
    from scripts import hermes_factory_watchdog as watchdog

    repo = _repo(tmp_path)
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        draft = repo / "draft.txt"
        draft.write_text("alpha")
        first = _probe(conn, watchdog, repo)
        draft.write_text("bravo")
        second = _probe(conn, watchdog, repo)
        assert first.complete and second.complete
        assert first.digest != second.digest

        if not hasattr(os, "symlink"):
            pytest.skip("symlinks unavailable")
        link = repo / "current"
        try:
            link.symlink_to("draft.txt")
        except OSError:
            pytest.skip("symlink creation unavailable")
        linked = _probe(conn, watchdog, repo)
        link.unlink()
        link.symlink_to("source.txt")
        retargeted = _probe(conn, watchdog, repo)
        assert linked.complete and retargeted.complete
        assert linked.digest != retargeted.digest
    finally:
        conn.close()


def test_semantic_probe_ignores_heartbeat_but_tracks_durable_progress(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb
    from scripts import hermes_factory_watchdog as watchdog
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect()
    task_id = kb.create_task(conn, title="probe", assignee="executor")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    task = kb.get_task(conn, task_id)
    run_id = task.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))
    try:
        baseline = watchdog._semantic_fingerprint(conn, task)
        heartbeat_result = json.loads(
            kanban_tools._handle_heartbeat({"note": "still working"})
        )
        assert heartbeat_result["ok"] is True
        heartbeat = watchdog._semantic_fingerprint(conn, task)
        assert heartbeat.digest == baseline.digest

        progress_result = json.loads(kanban_tools._handle_progress({
            "summary": "reviewed the storage boundary",
        }))
        assert progress_result["ok"] is True
        progress = watchdog._semantic_fingerprint(conn, task)
        assert progress.digest != heartbeat.digest
    finally:
        conn.close()


def test_semantic_probe_fails_closed_when_content_exceeds_bound(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from scripts import hermes_factory_watchdog as watchdog

    repo = _repo(tmp_path)
    (repo / "large.txt").write_text("too large")
    monkeypatch.setattr(watchdog, "MAX_RELEVANT_FILE_BYTES", 1)
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        probe = _probe(conn, watchdog, repo)
        assert probe.complete is False
        assert probe.error == "relevant_file_byte_limit"
    finally:
        conn.close()


def test_watchdog_emits_stable_current_snapshot(monkeypatch, tmp_path, capsys):
    from hermes_cli import kanban_db as kb
    from scripts import hermes_factory_watchdog as watchdog

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(watchdog, "STATE_PATH", tmp_path / "watchdog-state.json")
    monkeypatch.setattr(watchdog.kb, "list_boards", lambda: [{"slug": "default"}])

    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(conn, title="delivery watch", assignee="executor")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="buzz", chat_id="reader-channel",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET delivery_failures=12,"
                "delivery_last_error='invalid mention',delivery_quarantined_at=123 "
                "WHERE task_id=?",
                (task_id,),
            )
    finally:
        conn.close()

    assert watchdog.main() == 0
    first = capsys.readouterr().out
    assert "notification_quarantined" in first
    assert task_id in first

    # The cron monitor owns change detection. The source must keep emitting
    # the same active anomaly, otherwise JSON -> empty looks like recovery.
    assert watchdog.main() == 0
    assert capsys.readouterr().out == first


def test_cron_monitor_reports_one_anomaly_and_one_real_recovery(monkeypatch, tmp_path, capsys):
    from cron import monitor
    from hermes_cli import kanban_db as kb
    from scripts import hermes_factory_watchdog as watchdog

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(watchdog, "STATE_PATH", tmp_path / "watchdog-state.json")
    monkeypatch.setattr(watchdog.kb, "list_boards", lambda: [{"slug": "default"}])

    snapshots: list[str] = []

    def snapshot() -> str:
        assert watchdog.main() == 0
        value = capsys.readouterr().out
        snapshots.append(value)
        return value

    healthy = snapshot()
    conn = kb.connect(board="default")
    try:
        task_id = kb.create_task(conn, title="delivery watch", assignee="executor")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="buzz", chat_id="reader-channel",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET delivery_failures=12,"
                "delivery_last_error='invalid mention',delivery_quarantined_at=123 "
                "WHERE task_id=?",
                (task_id,),
            )
        anomaly = snapshot()
        same_anomaly = snapshot()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET delivery_failures=0,"
                "delivery_last_error=NULL,delivery_quarantined_at=NULL "
                "WHERE task_id=?",
                (task_id,),
            )
        recovered = snapshot()
    finally:
        conn.close()

    job = {"id": "factory-monitor", "monitor_state": None}
    old_output = {"value": ""}

    def persist(_job_id, new_hash, output):
        job["monitor_state"] = {"last_output_hash": new_hash}
        old_output["value"] = output
        return True

    monkeypatch.setattr(monitor, "_persist_monitor_state", persist)
    monkeypatch.setattr(monitor, "_read_last_output", lambda _job_id: old_output["value"])

    current = {"value": healthy}
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda _job: (True, current["value"]))
    baseline = monitor.check_monitor(job)
    assert baseline.first_run is True
    assert monitor.persist_monitor_outcome(job["id"], baseline) is True
    assert monitor.check_monitor(job).changed is False

    current["value"] = anomaly
    changed = monitor.check_monitor(job)
    assert changed.changed is True
    assert monitor.persist_monitor_outcome(job["id"], changed) is True
    current["value"] = same_anomaly
    assert monitor.check_monitor(job).changed is False
    current["value"] = recovered
    recovery = monitor.check_monitor(job)
    assert recovery.changed is True
    assert '"status": "healthy"' in recovery.context_block
