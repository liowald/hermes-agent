from __future__ import annotations


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

    monkeypatch.setattr(monitor, "_persist_monitor_state", persist)
    monkeypatch.setattr(monitor, "_read_last_output", lambda _job_id: old_output["value"])

    current = {"value": healthy}
    monkeypatch.setattr(monitor, "_run_monitor_source", lambda _job: (True, current["value"]))
    assert monitor.check_monitor(job).first_run is True
    assert monitor.check_monitor(job).changed is False

    current["value"] = anomaly
    assert monitor.check_monitor(job).changed is True
    current["value"] = same_anomaly
    assert monitor.check_monitor(job).changed is False
    current["value"] = recovered
    recovery = monitor.check_monitor(job)
    assert recovery.changed is True
    assert '"status": "healthy"' in recovery.context_block
