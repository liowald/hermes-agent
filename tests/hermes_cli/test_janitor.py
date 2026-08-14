from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import janitor
from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


def _complete_board(slug: str) -> str:
    kb.create_board(slug, name=slug)
    with kb.connect_closing(board=slug) as conn:
        task_id = kb.create_task(
            conn,
            title="completed canary",
            assignee="executor",
            initial_status="running",
            board=slug,
        )
        assert kb.complete_task(conn, task_id, result="verified")
    return task_id


def test_scan_is_stable_and_marks_completed_canary_recoverable(isolated_home):
    _complete_board("factory-canary-test")

    first = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    second = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )

    assert first == second
    assert first["summary"] == {"item_count": 1, "safety_counts": {"safe": 1}}
    item = first["items"][0]
    assert item["action"] == "archive_board"
    assert item["reversible"] is True


def test_scan_does_not_create_database_for_metadata_only_board(isolated_home):
    kb.write_board_metadata("empty-routing-board", name="Empty")
    board_dir = kb.board_dir("empty-routing-board")
    before = {
        path.relative_to(board_dir): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in board_dir.rglob("*") if path.is_file()
    }

    plan = janitor.scan_plan(
        boards=["empty-routing-board"], include_repos=False
    )

    after = {
        path.relative_to(board_dir): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in board_dir.rglob("*") if path.is_file()
    }
    assert before == after
    assert not (board_dir / "kanban.db").exists()
    assert plan["items"][0]["safety"] == "needs_review"


def test_scan_does_not_modify_existing_board_files(isolated_home):
    _complete_board("factory-canary-test")
    board_dir = kb.board_dir("factory-canary-test")
    before = {
        path.relative_to(board_dir): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in board_dir.rglob("*") if path.is_file()
    }

    janitor.scan_plan(boards=["factory-canary-test"], include_repos=False)

    after = {
        path.relative_to(board_dir): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in board_dir.rglob("*") if path.is_file()
    }
    assert before == after


def test_scan_rejects_direct_board_overrides(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "other.db"))

    with pytest.raises(ValueError, match="board-pinned"):
        janitor.scan_plan(include_repos=False)


@pytest.mark.parametrize(
    ("update", "expected_reason"),
    [
        ({"subscriptions": 1}, "notification subscriptions"),
        ({"active_delivery_claims": 1}, "notification subscriptions"),
        ({"active_runs": 1}, "active run state"),
        ({"tasks": [{"current_run_id": 9}]}, "active run state"),
        ({"workflows": [{"state": "completing"}]}, "has not reached done"),
    ],
)
def test_archive_policy_fails_closed_on_active_durable_state(update, expected_reason):
    snapshot = {
        "counts": {"done": 1},
        "tasks": [{"current_run_id": None}],
        "subscriptions": 0,
        "active_delivery_claims": 0,
        "active_runs": 0,
        "workflows": [{"state": "done"}],
    }
    snapshot.update(update)

    safety, action, reason = janitor._board_archive_policy(
        "factory-canary-test", snapshot
    )

    assert safety != "safe"
    assert action != "archive_board"
    assert expected_reason in reason


def test_apply_archives_and_restore_recovers_board(isolated_home):
    task_id = _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )

    result = janitor.apply_plan(plan)

    assert result["summary"] == {"applied": 1}
    archived = Path(result["results"][0]["archive_path"])
    assert archived.is_dir()
    assert not kb.board_exists("factory-canary-test")

    restored = janitor.restore_board(str(archived), "factory-canary-test")
    assert restored["action"] == "restored"
    with kb.connect_closing(board="factory-canary-test") as conn:
        assert kb.get_task(conn, task_id).status == "done"


def test_archive_waits_for_live_connection_and_prevents_slug_recreation(
    isolated_home,
):
    task_id = _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    live = kb.connect(board="factory-canary-test")
    outcome: dict[str, object] = {}

    def apply() -> None:
        outcome["result"] = janitor.apply_plan(plan)

    thread = threading.Thread(target=apply)
    thread.start()
    marker = kb.board_maintenance_marker("factory-canary-test")
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    assert thread.is_alive()
    with pytest.raises(RuntimeError, match="exclusive maintenance"):
        kb.connect(board="factory-canary-test")

    live.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    result = outcome["result"]
    assert isinstance(result, dict)
    archived = Path(result["results"][0]["archive_path"])
    assert archived.is_dir()
    assert not kb.board_dir("factory-canary-test").exists()
    with pytest.raises(RuntimeError, match="exclusive maintenance"):
        kb.connect(board="factory-canary-test")
    assert not kb.board_dir("factory-canary-test").exists()

    janitor.restore_board(str(archived), "factory-canary-test")
    with kb.connect_closing(board="factory-canary-test") as conn:
        assert kb.get_task(conn, task_id).status == "done"


def test_archive_serializes_metadata_write_and_rejects_changed_fingerprint(
    isolated_home, monkeypatch
):
    _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    entered = threading.Event()
    release = threading.Event()
    original_write_text = Path.write_text

    def paused_write(path: Path, data: str, *args, **kwargs):
        if path == kb.board_metadata_path("factory-canary-test"):
            entered.set()
            assert release.wait(timeout=2)
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", paused_write)
    metadata_thread = threading.Thread(
        target=lambda: kb.write_board_metadata(
            "factory-canary-test", description="changed"
        )
    )
    metadata_thread.start()
    assert entered.wait(timeout=2)
    outcome: dict[str, object] = {}
    archive_thread = threading.Thread(
        target=lambda: outcome.setdefault("result", janitor.apply_plan(plan))
    )
    archive_thread.start()
    assert archive_thread.is_alive()

    release.set()
    metadata_thread.join(timeout=2)
    archive_thread.join(timeout=2)
    assert not metadata_thread.is_alive()
    assert not archive_thread.is_alive()
    result = outcome["result"]
    assert isinstance(result, dict)
    assert result["summary"] == {"rejected": 1}
    assert kb.board_exists("factory-canary-test")


def test_archive_final_tombstone_failure_remains_fail_closed(
    isolated_home, monkeypatch
):
    _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    original_write = janitor._write_tombstone
    calls = 0

    def fail_final(path: Path, text: str, *, create: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated final tombstone failure")
        original_write(path, text, create=create)

    monkeypatch.setattr(janitor, "_write_tombstone", fail_final)
    with pytest.raises(OSError, match="simulated"):
        janitor.apply_plan(plan)

    tombstone = kb.board_archive_tombstone("factory-canary-test")
    assert tombstone.read_text().startswith("pending:")
    assert not kb.board_dir("factory-canary-test").exists()
    with pytest.raises(RuntimeError, match="exclusive maintenance"):
        kb.connect(board="factory-canary-test")
    assert not kb.board_dir("factory-canary-test").exists()

    archived = next(
        (kb.boards_root() / "_archived").glob("factory-canary-test-*")
    )
    dead_owner = subprocess.Popen(["true"])
    dead_owner.wait(timeout=2)
    kb.board_maintenance_marker("factory-canary-test").write_text(
        f"{dead_owner.pid}\n", encoding="utf-8"
    )
    janitor.restore_board(str(archived), "factory-canary-test")
    assert kb.board_exists("factory-canary-test")


def test_apply_requires_gateway_stopped(isolated_home, monkeypatch):
    _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    monkeypatch.setattr(janitor, "_gateway_pid", lambda: 1234)

    with pytest.raises(ValueError, match="gateway to be stopped"):
        janitor.apply_plan(plan)

    assert kb.board_exists("factory-canary-test")


def test_mixed_mutating_plan_is_rejected_before_archive(isolated_home):
    _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    plan["items"].append(
        {
            "resource_kind": "git_worktree_metadata",
            "resource_id": "/tmp/repo",
            "action": "prune_worktree_metadata",
            "safety": "safe",
            "reversible": False,
            "fingerprint": "unused",
        }
    )

    with pytest.raises(ValueError, match="exactly one mutating"):
        janitor.apply_plan(plan)

    assert kb.board_exists("factory-canary-test")


def test_apply_rejects_changed_fingerprint(isolated_home):
    _complete_board("factory-canary-test")
    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )
    with kb.connect_closing(board="factory-canary-test") as conn:
        kb.create_task(
            conn,
            title="new active work",
            assignee="executor",
            initial_status="running",
            board="factory-canary-test",
        )

    result = janitor.apply_plan(plan)

    assert result["summary"] == {"rejected": 1}
    assert "rescan" in result["results"][0]["error"]
    assert kb.board_exists("factory-canary-test")


def test_nonterminal_board_is_never_an_archive_action(isolated_home):
    kb.create_board("factory-canary-test", name="test")
    with kb.connect_closing(board="factory-canary-test") as conn:
        kb.create_task(
            conn,
            title="active",
            assignee="executor",
            initial_status="running",
            board="factory-canary-test",
        )

    plan = janitor.scan_plan(
        boards=["factory-canary-test"], include_repos=False
    )

    item = plan["items"][0]
    assert item["safety"] == "must_preserve"
    assert item["action"] == "none"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_repo_scan_reports_clean_merged_worktree_but_preserves_dirty_one(
    isolated_home, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-b", "cleanup-candidate", str(worktree), "HEAD")

    clean_plan = janitor.scan_plan(
        boards=[], repos=[str(repo)], include_repos=True, actionable_only=True
    )
    clean_item = next(
        item for item in clean_plan["items"]
        if item["resource_kind"] == "git_worktree"
    )
    assert clean_item["safety"] == "needs_review"
    assert clean_item["action"] == "review_worktree_cleanup"

    (worktree / "tracked.txt").write_text("dirty\n")
    dirty_plan = janitor.scan_plan(
        boards=[], repos=[str(repo)], include_repos=True
    )
    dirty_item = next(
        item for item in dirty_plan["items"]
        if item["resource_kind"] == "git_worktree"
    )
    assert dirty_item["safety"] == "must_preserve"
    assert dirty_item["action"] == "none"


def test_repo_scan_does_not_refresh_git_index(isolated_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    index = repo / ".git" / "index"
    before = (index.stat().st_size, index.stat().st_mtime_ns)

    janitor.scan_plan(boards=[], repos=[str(repo)], include_repos=True)

    assert (index.stat().st_size, index.stat().st_mtime_ns) == before


def test_restore_requires_direct_matching_archive_child(isolated_home, tmp_path):
    archive_root = kb.boards_root() / "_archived"
    nested = archive_root / "outer" / "factory-canary-test-1"
    nested.mkdir(parents=True)
    (nested / "board.json").write_text("{}")

    with pytest.raises(ValueError, match="direct child"):
        janitor.restore_board(str(nested), "factory-canary-test")


def test_parser_requires_explicit_confirmation_for_apply():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    janitor.build_parser(subparsers)

    args = parser.parse_args(["janitor", "apply", "--plan", "/tmp/plan.json"])
    assert args.yes is False
    assert args.func is janitor.cmd_janitor
