from __future__ import annotations

import concurrent.futures
import json
import subprocess
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_factory as factory


@pytest.fixture
def factory_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    for name in ("executor", "reviewer-a", "reviewer-b", "fixer"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("model:\n  default: test\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "factory@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Factory Test"], cwd=repo, check=True)
    (repo / "app.txt").write_text("before\n")
    subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/project.git"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo, check=True,
    )
    return home, repo


def _claim_complete(conn, task_id, metadata):
    claimed = kb.claim_task(conn, task_id, claimer=f"test-{task_id}")
    assert claimed is not None
    assert kb.complete_task(
        conn,
        task_id,
        summary="phase complete",
        metadata=metadata,
        expected_run_id=claimed.current_run_id,
    )


def test_factory_root_rejects_false_completion_and_requires_dual_quorum(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn,
            title="Change app",
            body="Change app.txt and prepare a local delivery receipt.",
            workspace_kind="dir",
            workspace_path=str(repo),
            idempotency_key="feature:change-app",
            delivery_mode="local_commit",
        )
        root = created["root_id"]
        with pytest.raises(Exception, match="factory root is not verified"):
            kb.complete_task(conn, root, summary="looks good")

        implement = created["implement_task_id"]
        (repo / "app.txt").write_text("after\n")
        _claim_complete(conn, implement, {
            "inspection_only": False,
            "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        reviewing = factory.reconcile_factory(conn, root)
        assert reviewing["state"] == "reviewing"
        candidate = reviewing["candidate_sha"]
        assert kb.get_task(conn, reviewing["reviewer_a_task_id"]).worker_toolsets == [
            "factory_review_readonly"
        ]
        assert kb.get_task(conn, reviewing["reviewer_b_task_id"]).worker_toolsets == [
            "factory_review_readonly"
        ]

        _claim_complete(conn, reviewing["reviewer_a_task_id"], {
            "verdict": "approve", "candidate_sha": candidate,
            "findings": [], "verification": ["bundle inspected"],
        })
        assert factory.reconcile_factory(conn, root)["state"] == "reviewing"
        assert kb.get_task(conn, root).status == "blocked"

        _claim_complete(conn, reviewing["reviewer_b_task_id"], {
            "verdict": "approve", "candidate_sha": candidate,
            "findings": [], "verification": ["bundle inspected"],
        })
        delivering = factory.reconcile_factory(conn, root)
        assert delivering["state"] == "delivering"
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "change app"], cwd=repo, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        _claim_complete(conn, delivering["delivery_task_id"], {
            "candidate_sha": candidate,
            "tests_run": ["unit"],
            "git_status": "clean",
            "commit_sha": head,
            "delivery": {"kind": "local_commit", "receipt": "ok"},
        })
        done = factory.reconcile_factory(conn, root)
        assert done["state"] == "done"
        assert kb.get_task(conn, root).status == "done"
    finally:
        conn.close()


def test_factory_phase_rejects_ordinary_same_card_review_handoff(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Separate review cards", body="Keep factory review isolated.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:no-same-card-review",
            delivery_mode="local_commit",
        )
        task_id = created["implement_task_id"]
        claimed = kb.claim_task(conn, task_id, claimer="factory-executor")
        assert claimed is not None

        ok, reason = kb.request_review(
            conn, task_id, summary="I should not review myself.",
            expected_run_id=claimed.current_run_id, with_reason=True,
        )

        assert not ok
        assert "must call complete or block" in reason
        assert "separate reviewer cards" in reason
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.latest_run(conn, task_id).status == "running"
    finally:
        conn.close()


def test_review_changes_routes_only_to_fixer_and_invalidates_prior_approvals(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn,
            title="Repair app",
            body="Repair app.txt.",
            workspace_kind="dir",
            workspace_path=str(repo),
            idempotency_key="feature:repair-app",
        )
        root = created["root_id"]
        (repo / "app.txt").write_text("candidate one\n")
        _claim_complete(conn, created["implement_task_id"], {
            "inspection_only": False, "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        review = factory.reconcile_factory(conn, root)
        candidate = review["candidate_sha"]
        _claim_complete(conn, review["reviewer_a_task_id"], {
            "verdict": "changes", "candidate_sha": candidate,
            "findings": ["wrong value"], "verification": ["bundle inspected"],
        })
        _claim_complete(conn, review["reviewer_b_task_id"], {
            "verdict": "approve", "candidate_sha": candidate,
            "findings": [], "verification": ["bundle inspected"],
        })
        fixing = factory.reconcile_factory(conn, root)
        assert fixing["state"] == "fixing"
        fixer_task = kb.get_task(conn, fixing["fixer_task_id"])
        assert fixer_task.assignee == "fixer"

        (repo / "app.txt").write_text("candidate two\n")
        _claim_complete(conn, fixer_task.id, {
            "inspection_only": False, "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        rereview = factory.reconcile_factory(conn, root)
        assert rereview["state"] == "reviewing"
        assert rereview["cycle"] == 1
        assert rereview["candidate_sha"] != candidate
        assert rereview["reviewer_a_task_id"] != review["reviewer_a_task_id"]
        assert rereview["reviewer_b_task_id"] != review["reviewer_b_task_id"]
    finally:
        conn.close()


def test_factory_roles_must_be_distinct(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="must be distinct"):
            factory.create_factory(
                conn,
                title="Unsafe",
                body="No shared role.",
                workspace_kind="dir",
                workspace_path=str(repo),
                reviewer_b="reviewer-a",
                idempotency_key="feature:duplicate-reviewer-role",
            )
    finally:
        conn.close()


def test_factory_creation_rolls_back_whole_graph_on_phase_failure(factory_env, monkeypatch):
    _, repo = factory_env
    conn = kb.connect()
    original = kb.create_task
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("phase insert failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "create_task", fail_second)
    try:
        with pytest.raises(RuntimeError, match="phase insert failed"):
            factory.create_factory(
                conn,
                title="Atomic",
                body="All or nothing.",
                workspace_kind="dir",
                workspace_path=str(repo),
                idempotency_key="feature:atomic",
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM factory_workflows").fetchone()[0] == 0
    finally:
        conn.close()


def test_factory_refuses_reserved_key_owned_by_plain_task(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        kb.create_task(
            conn,
            title="Unrelated",
            assignee="executor",
            idempotency_key="hermes-factory-root:feature:collision",
        )
        with pytest.raises(ValueError, match="unguarded task"):
            factory.create_factory(
                conn,
                title="Collision",
                body="Must not attach to unrelated task.",
                workspace_kind="dir",
                workspace_path=str(repo),
                idempotency_key="feature:collision",
            )
        assert conn.execute("SELECT COUNT(*) FROM factory_workflows").fetchone()[0] == 0
    finally:
        conn.close()


def test_candidate_tree_includes_executable_mode(factory_env):
    _, repo = factory_env
    before = factory._workspace_tree_sha(repo)
    (repo / "app.txt").chmod(0o755)
    assert factory._workspace_tree_sha(repo) != before


def test_draft_pr_delivery_is_verified_against_code_host(monkeypatch):
    head = "a" * 40
    url = "https://github.com/example/project/pull/42"
    def fake_run(argv, **kwargs):
        if argv == ["gh", "repo", "view", "example/project", "--json", "defaultBranchRef"]:
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=json.dumps({"defaultBranchRef": {"name": "main"}}),
                stderr="",
            )
        assert argv[:4] == ["gh", "pr", "view", url]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({
                "url": url,
                "state": "OPEN",
                "isDraft": True,
                "headRefOid": head,
                "baseRefName": "main",
            }),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert factory._verify_draft_pr_delivery(
        {"kind": "draft_pr", "url": url, "head_sha": head},
        head, "example/project", "main",
    ) is None
    assert "does not match" in factory._verify_draft_pr_delivery(
        {"kind": "draft_pr", "url": url, "head_sha": "b" * 40},
        head, "example/project", "main",
    )
    assert "repository does not match" in factory._verify_draft_pr_delivery(
        {
            "kind": "draft_pr",
            "url": "https://github.com/example/other/pull/7",
            "head_sha": head,
        },
        head, "example/project", "main",
    )


def test_factory_intake_binds_delivery_repository_and_base(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Bound target", body="Keep the intake target immutable.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:bound-target",
        )
        (repo / "app.txt").write_text("candidate\n")
        _claim_complete(conn, created["implement_task_id"], {
            "inspection_only": False, "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/example/other.git"],
            cwd=repo, check=True,
        )
        blocked = factory.reconcile_factory(conn, created["root_id"])
        assert blocked["state"] == "blocked"
        assert "target changed after factory intake" in blocked["last_error"]
    finally:
        conn.close()


def test_factory_rejects_dirty_or_non_default_intake(factory_env):
    _, repo = factory_env
    (repo / "app.txt").write_text("uncommitted\n")
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="clean checkout at the exact"):
            factory.create_factory(
                conn, title="Unsafe base", body="Do not inherit unrelated work.",
                workspace_kind="dir", workspace_path=str(repo),
                idempotency_key="feature:unsafe-base",
            )
        assert conn.execute("SELECT COUNT(*) FROM factory_workflows").fetchone()[0] == 0
    finally:
        conn.close()


def test_local_commit_review_bundle_is_based_on_factory_intake(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Review committed change", body="Review the complete commit diff.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:committed-before-review",
            delivery_mode="local_commit",
        )
        intake_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        assert created["delivery_base_sha"] == intake_sha

        (repo / "app.txt").write_text("committed candidate\n")
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "committed candidate"], cwd=repo,
            check=True, capture_output=True,
        )
        candidate_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        _claim_complete(conn, created["implement_task_id"], {
            "inspection_only": False,
            "changed_files": ["app.txt"],
            "tests_run": ["unit"],
            "commit_sha": candidate_head,
        })

        reviewing = factory.reconcile_factory(conn, created["root_id"])
        assert reviewing["state"] == "reviewing"
        bundle = json.loads(Path(reviewing["review_bundle_path"]).read_text())
        assert bundle["base_sha"] == intake_sha
        assert "-before" in bundle["diff"]
        assert "+committed candidate" in bundle["diff"]
    finally:
        conn.close()


def test_factory_idempotent_retry_returns_existing_root_after_workspace_changes(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Retry-safe intake", body="Preserve the first receipt.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:lost-create-response",
            delivery_mode="local_commit",
        )
        (repo / "app.txt").write_text("implementation in progress\n")

        retried = factory.create_factory(
            conn, title="Retry-safe intake", body="Preserve the first receipt.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:lost-create-response",
            delivery_mode="local_commit",
        )

        assert retried["root_id"] == created["root_id"]
        assert retried["implement_task_id"] == created["implement_task_id"]
        assert conn.execute("SELECT COUNT(*) FROM factory_workflows").fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_reconcilers_create_only_one_fixer(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Single fixer", body="Do not duplicate phase writers.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:single-fixer", delivery_mode="local_commit",
        )
        (repo / "app.txt").write_text("candidate\n")
        _claim_complete(conn, created["implement_task_id"], {
            "inspection_only": False, "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        review = factory.reconcile_factory(conn, created["root_id"])
        for task_id, verdict in (
            (review["reviewer_a_task_id"], "changes"),
            (review["reviewer_b_task_id"], "approve"),
        ):
            _claim_complete(conn, task_id, {
                "verdict": verdict, "candidate_sha": review["candidate_sha"],
                "findings": ["repair"] if verdict == "changes" else [],
                "verification": ["bundle inspected"],
            })
        root_id = created["root_id"]
    finally:
        conn.close()

    def reconcile_once():
        thread_conn = kb.connect()
        try:
            return factory.reconcile_factory(thread_conn, root_id)
        finally:
            thread_conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reconcile_once(), range(2)))
    assert {result["state"] for result in results} == {"fixing"}
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT fixer_task_id FROM factory_workflows WHERE root_id=?", (root_id,)
        ).fetchone()
        tasks = conn.execute(
            "SELECT id FROM tasks WHERE created_by=? AND title LIKE 'Fix review findings:%'",
            (root_id,),
        ).fetchall()
        assert [task["id"] for task in tasks] == [row["fixer_task_id"]]
    finally:
        conn.close()


def test_blocked_phase_is_visible_and_explicitly_retryable(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Retry", body="Recover a worker block.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:retry",
        )
        task = kb.claim_task(conn, created["implement_task_id"])
        assert task is not None
        assert kb.block_task(
            conn, task.id, reason="temporary", kind="transient",
            expected_run_id=task.current_run_id,
        )
        blocked = factory.reconcile_factory(conn, created["root_id"])
        assert blocked["state"] == "blocked"
        assert "entered blocked" in blocked["last_error"]
        retried = factory.retry_factory(conn, created["root_id"])
        assert retried["state"] == "implementing"
        assert kb.get_task(conn, task.id).status == "ready"
    finally:
        conn.close()


def test_terminal_invalid_delivery_retries_with_one_replacement(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Retry receipt", body="Deliver one reviewed commit.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:retry-terminal-receipt",
            delivery_mode="local_commit",
        )
        (repo / "app.txt").write_text("after\n")
        _claim_complete(conn, created["implement_task_id"], {
            "inspection_only": False,
            "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        reviewing = factory.reconcile_factory(conn, created["root_id"])
        candidate = reviewing["candidate_sha"]
        for task_id in (
            reviewing["reviewer_a_task_id"], reviewing["reviewer_b_task_id"],
        ):
            _claim_complete(conn, task_id, {
                "verdict": "approve",
                "candidate_sha": candidate,
                "findings": [],
                "verification": ["bundle inspected"],
            })
        delivering = factory.reconcile_factory(conn, created["root_id"])
        original_delivery = delivering["delivery_task_id"]
        original_task = kb.get_task(conn, original_delivery)
        assert "whose kind is exactly" in original_task.body
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "change app"], cwd=repo,
            check=True, capture_output=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        _claim_complete(conn, original_delivery, {
            "candidate_sha": candidate,
            "tests_run": ["unit"],
            "git_status": "clean",
            "commit_sha": head,
            "delivery": {"mode": "local_commit", "receipt": "wrong key"},
        })
        blocked = factory.reconcile_factory(conn, created["root_id"])
        assert blocked["state"] == "blocked"
        assert blocked["blocked_from_state"] == "delivering"

        retried = factory.retry_factory(conn, created["root_id"])
        replacement = retried["delivery_task_id"]
        assert retried["state"] == "delivering"
        assert replacement != original_delivery
        assert kb.get_task(conn, original_delivery).status == "done"
        replacement_task = kb.get_task(conn, replacement)
        assert replacement_task.status == "ready"
        assert replacement_task.workspace_path == original_task.workspace_path
        assert "delivery receipt does not match authorized mode" in replacement_task.body
        assert "delivery.kind exactly 'local_commit'" in replacement_task.body
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE ?",
            (f"factory:{created['root_id']}:retry:delivering:%",),
        ).fetchone()[0] == 1

        _claim_complete(conn, replacement, {
            "candidate_sha": candidate,
            "tests_run": ["unit"],
            "git_status": "clean",
            "commit_sha": head,
            "delivery": {"kind": "local_commit", "receipt": "ok"},
        })
        done = factory.reconcile_factory(conn, created["root_id"])
        assert done["state"] == "done"
        assert kb.get_task(conn, created["root_id"]).status == "done"
    finally:
        conn.close()


def test_terminal_invalid_review_retry_preserves_read_only_role(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Retry review", body="Keep reviewer capabilities bounded.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:retry-terminal-review",
            delivery_mode="local_commit",
        )
        (repo / "app.txt").write_text("after\n")
        _claim_complete(conn, created["implement_task_id"], {
            "inspection_only": False,
            "changed_files": ["app.txt"],
            "tests_run": ["unit"],
        })
        reviewing = factory.reconcile_factory(conn, created["root_id"])
        original_a = reviewing["reviewer_a_task_id"]
        original_b = reviewing["reviewer_b_task_id"]
        _claim_complete(conn, original_a, {
            "verdict": "approve",
            "candidate_sha": reviewing["candidate_sha"],
            "findings": [],
            "verification": [],
        })
        blocked = factory.reconcile_factory(conn, created["root_id"])
        assert blocked["state"] == "blocked"

        retried = factory.retry_factory(conn, created["root_id"])
        replacement_a = retried["reviewer_a_task_id"]
        assert retried["state"] == "reviewing"
        assert replacement_a != original_a
        assert retried["reviewer_b_task_id"] == original_b
        assert kb.get_task(conn, replacement_a).worker_toolsets == [
            "factory_review_readonly"
        ]
        assert kb.get_task(conn, original_b).status == "ready"
    finally:
        conn.close()


def test_completing_state_resumes_idempotently_after_crash(factory_env):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Resume", body="Resume terminal transition.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:resume",
        )
        receipt = {
            "contract": "hermes.factory.receipt.v1",
            "candidate_sha": "tree",
            "reviewer_a": "reviewer-a",
            "reviewer_b": "reviewer-b",
            "delivery": {"kind": "local_commit"},
            "delivery_mode": "local_commit",
            "commit_sha": "a" * 40,
            "tests_run": ["unit"],
        }
        raw = json.dumps(receipt, sort_keys=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE factory_workflows SET state='completing',final_receipt=? WHERE root_id=?",
                (raw, created["root_id"]),
            )
        done = factory.reconcile_factory(conn, created["root_id"])
        assert done["state"] == "done"
        assert kb.get_task(conn, created["root_id"]).result == raw
        assert factory.reconcile_factory(conn, created["root_id"])["state"] == "done"
    finally:
        conn.close()


def test_concurrent_terminal_reconcile_cannot_regress_done_root(factory_env, monkeypatch):
    _, repo = factory_env
    conn = kb.connect()
    try:
        created = factory.create_factory(
            conn, title="Terminal race", body="Complete once.",
            workspace_kind="dir", workspace_path=str(repo),
            idempotency_key="feature:terminal-race", delivery_mode="local_commit",
        )
        receipt = {
            "contract": "hermes.factory.receipt.v1",
            "candidate_sha": "tree",
            "reviewer_a": "reviewer-a",
            "reviewer_b": "reviewer-b",
            "delivery": {"kind": "local_commit"},
            "delivery_mode": "local_commit",
            "commit_sha": "a" * 40,
            "tests_run": ["unit"],
        }
        raw = json.dumps(receipt, sort_keys=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE factory_workflows SET state='completing',final_receipt=? WHERE root_id=?",
                (raw, created["root_id"]),
            )
        root_id = created["root_id"]
    finally:
        conn.close()

    barrier = threading.Barrier(2)
    real_complete = kb.complete_task

    def synchronized_complete(*args, **kwargs):
        barrier.wait(timeout=5)
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(kb, "complete_task", synchronized_complete)

    def reconcile_once():
        thread_conn = kb.connect()
        try:
            return factory.reconcile_factory(thread_conn, root_id)
        finally:
            thread_conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reconcile_once(), range(2)))
    assert {result["state"] for result in results} == {"done"}
    conn = kb.connect()
    try:
        workflow = factory.inspect_factory(conn, root_id)
        assert workflow["state"] == "done"
        assert workflow["last_error"] is None
        assert kb.get_task(conn, root_id).status == "done"
    finally:
        conn.close()
