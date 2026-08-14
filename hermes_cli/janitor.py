"""Deterministic, policy-bounded cleanup planning for Hermes state.

Scanning is read-only. Applying a plan revalidates every state fingerprint and
currently permits only recoverable board archives by default. Ambiguous or
irreversible resources remain review candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb


PLAN_CONTRACT = "hermes.janitor.plan.v1"
TERMINAL_TASK_STATUSES = {"done", "archived"}
ACTIONABLE_SAFETY = {"safe", "needs_review"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _reject_board_overrides() -> None:
    pinned = [
        name
        for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")
        if os.environ.get(name, "").strip()
    ]
    if pinned:
        raise ValueError(
            "janitor refuses board-pinned environments; unset " + ", ".join(pinned)
        )


def _explicit_board_db_path(slug: str) -> Path:
    if slug == kb.DEFAULT_BOARD:
        return kb.kanban_home() / "kanban.db"
    return kb.board_dir(slug) / "kanban.db"


@contextmanager
def _readonly_db(path: Path):
    """Open an existing SQLite DB without schema, WAL, or migration writes."""
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _board_snapshot(slug: str) -> dict[str, Any]:
    meta = kb.read_board_metadata(slug)
    metadata = {
        key: value for key, value in meta.items() if key != "db_path"
    }
    db_path = _explicit_board_db_path(slug)
    if not db_path.is_file():
        return {
            "slug": slug,
            "metadata": metadata,
            "name": meta.get("name", ""),
            "created_at": meta.get("created_at"),
            "default_workdir": meta.get("default_workdir"),
            "project_id": meta.get("project_id"),
            "counts": {},
            "tasks": [],
            "subscriptions": 0,
            "active_delivery_claims": 0,
            "active_runs": 0,
            "workflows": [],
        }
    with _readonly_db(db_path) as conn:
        task_rows = conn.execute(
            "SELECT id,status,current_run_id,workspace_kind,workspace_path "
            "FROM tasks ORDER BY id"
        ).fetchall()
        tasks = [dict(row) for row in task_rows]
        counts: dict[str, int] = {}
        for row in tasks:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        subscriptions = 0
        active_delivery_claims = 0
        if _table_exists(conn, "kanban_notify_subs"):
            subscriptions = int(
                conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0]
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")
            }
            if "delivery_claim_token" in columns:
                active_delivery_claims = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM kanban_notify_subs "
                        "WHERE delivery_claim_token IS NOT NULL"
                    ).fetchone()[0]
                )
        active_runs = 0
        if _table_exists(conn, "task_runs"):
            active_runs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM task_runs WHERE status='running'"
                ).fetchone()[0]
            )
        workflows: list[dict[str, Any]] = []
        if _table_exists(conn, "factory_workflows"):
            workflows = [
                dict(row)
                for row in conn.execute(
                    "SELECT root_id,state,contract_version FROM factory_workflows "
                    "ORDER BY root_id"
                ).fetchall()
            ]
    return {
        "slug": slug,
        "metadata": metadata,
        "name": meta.get("name", ""),
        "created_at": meta.get("created_at"),
        "default_workdir": meta.get("default_workdir"),
        "project_id": meta.get("project_id"),
        "counts": dict(sorted(counts.items())),
        "tasks": tasks,
        "subscriptions": subscriptions,
        "active_delivery_claims": active_delivery_claims,
        "active_runs": active_runs,
        "workflows": workflows,
    }


def _board_archive_policy(slug: str, snapshot: dict[str, Any]) -> tuple[str, str, str]:
    counts = snapshot["counts"]
    total = sum(counts.values())
    nonterminal = sum(
        count for status, count in counts.items() if status not in TERMINAL_TASK_STATUSES
    )
    dangling_task_runs = any(task.get("current_run_id") is not None for task in snapshot["tasks"])
    workflows_done = all(row.get("state") == "done" for row in snapshot["workflows"])

    if slug == kb.DEFAULT_BOARD:
        return "must_preserve", "none", "the default board cannot be archived"
    if total == 0:
        return "needs_review", "review_board_archive", "empty named board may be an intentional routing anchor"
    if nonterminal:
        return "must_preserve", "none", f"board has {nonterminal} nonterminal task(s)"
    if dangling_task_runs or snapshot["active_runs"]:
        return "must_preserve", "none", "terminal cards still reference active run state"
    if not workflows_done:
        return "must_preserve", "none", "factory workflow has not reached done"
    if snapshot["subscriptions"] or snapshot["active_delivery_claims"]:
        return "needs_review", "review_board_archive", "notification subscriptions must be removed or preserved explicitly"
    if slug.startswith(("factory-canary-", "canary-", "test-")):
        return "safe", "archive_board", "completed disposable board; archive is recoverable"
    return "needs_review", "review_board_archive", "all tasks are terminal, but board intent is not disposable by policy"


def _board_items(board_filter: Optional[set[str]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for meta in kb.list_boards(include_archived=False):
        slug = str(meta["slug"])
        if board_filter is not None and slug not in board_filter:
            continue
        snapshot = _board_snapshot(slug)
        counts = snapshot["counts"]
        safety, action, reason = _board_archive_policy(slug, snapshot)

        items.append(
            {
                "resource_kind": "kanban_board",
                "resource_id": slug,
                "action": action,
                "safety": safety,
                "reversible": action == "archive_board",
                "reason": reason,
                "fingerprint": _fingerprint(snapshot),
                "details": {
                    "counts": counts,
                    "subscriptions": snapshot["subscriptions"],
                    "active_delivery_claims": snapshot["active_delivery_claims"],
                    "active_runs": snapshot["active_runs"],
                    "workflow_count": len(snapshot["workflows"]),
                    "default_workdir": snapshot["default_workdir"],
                },
            }
        )

        backlog = sum(counts.get(s, 0) for s in ("triage", "todo", "blocked", "scheduled"))
        if backlog:
            card_state = {"slug": slug, "counts": counts, "backlog": backlog}
            items.append(
                {
                    "resource_kind": "kanban_cards",
                    "resource_id": slug,
                    "action": "review_cards",
                    "safety": "needs_review",
                    "reversible": False,
                    "reason": f"{backlog} parked or blocked card(s) require intent review",
                    "fingerprint": _fingerprint(card_state),
                    "details": {"counts": counts},
                }
            )
    return items


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=30,
        check=check,
        env=env,
    )


def _git_root(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    proc = _git(path, "rev-parse", "--show-toplevel")
    if proc.returncode:
        return None
    try:
        top = Path(proc.stdout.strip()).resolve()
        common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode == 0:
            common_path = Path(common.stdout.strip()).resolve()
            if common_path.name == ".git" and common_path.parent.is_dir():
                return common_path.parent
        return top
    except OSError:
        return None


def _discover_repos(explicit: Optional[Iterable[str]]) -> list[Path]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.extend(Path(value).expanduser() for value in explicit)
    else:
        candidates.append(Path.cwd())
        for meta in kb.list_boards(include_archived=False):
            workdir = meta.get("default_workdir")
            if workdir:
                candidates.append(Path(str(workdir)).expanduser())
        try:
            from hermes_cli import projects_db

            with projects_db.connect_closing() as conn:
                for project in projects_db.list_projects(conn):
                    candidates.append(Path(project.primary_path).expanduser())
        except Exception:
            pass

    roots: dict[str, Path] = {}
    for candidate in candidates:
        root = _git_root(candidate)
        if root is not None:
            roots[str(root)] = root
    return [roots[key] for key in sorted(roots)]


def _worktree_records(repo: Path) -> list[dict[str, str]]:
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode:
        return []
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*proc.stdout.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def _repo_items(repos: Iterable[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for repo in repos:
        primary_branch_proc = _git(repo, "symbolic-ref", "--short", "HEAD")
        primary_branch = primary_branch_proc.stdout.strip() if primary_branch_proc.returncode == 0 else ""
        base_ref = primary_branch or "HEAD"
        repo_head_proc = _git(repo, "rev-parse", base_ref)
        if repo_head_proc.returncode:
            continue
        repo_head = repo_head_proc.stdout.strip()
        records = _worktree_records(repo)
        stale = [row for row in records if row.get("worktree") and not Path(row["worktree"]).exists()]
        if stale:
            state = {
                "repo": str(repo),
                "stale": [
                    {"worktree": row.get("worktree"), "gitdir": row.get("gitdir", "")}
                    for row in stale
                ],
            }
            items.append(
                {
                    "resource_kind": "git_worktree_metadata",
                    "resource_id": str(repo),
                    "action": "prune_worktree_metadata",
                    "safety": "safe",
                    "reversible": False,
                    "reason": f"{len(stale)} registered worktree path(s) no longer exist",
                    "fingerprint": _fingerprint(state),
                    "details": {"paths": [row["worktree"] for row in stale]},
                }
            )

        checked_out_branches: set[str] = set()
        primary = repo.resolve()
        for row in records:
            raw_path = row.get("worktree")
            if not raw_path:
                continue
            path = Path(raw_path)
            branch_ref = row.get("branch", "")
            branch = branch_ref.removeprefix("refs/heads/") if branch_ref else ""
            if branch:
                checked_out_branches.add(branch)
            if not path.exists() or path.resolve() == primary:
                continue
            status = _git(path, "status", "--porcelain=v1").stdout
            dirty = bool(status.strip())
            head = row.get("HEAD", "")
            merged = bool(head) and _git(repo, "merge-base", "--is-ancestor", head, repo_head).returncode == 0
            state = {
                "repo": str(repo),
                "path": str(path.resolve()),
                "head": head,
                "branch": branch,
                "dirty": dirty,
                "merged_into_repo_head": merged,
            }
            if dirty:
                safety = "must_preserve"
                reason = "worktree has uncommitted changes"
            elif not merged:
                safety = "must_preserve"
                reason = "worktree commit is not merged into the repository HEAD"
            else:
                safety = "needs_review"
                reason = "clean worktree is merged; confirm task and branch ownership before removal"
            items.append(
                {
                    "resource_kind": "git_worktree",
                    "resource_id": str(path.resolve()),
                    "action": "review_worktree_cleanup" if safety == "needs_review" else "none",
                    "safety": safety,
                    "reversible": False,
                    "reason": reason,
                    "fingerprint": _fingerprint(state),
                    "details": state,
                }
            )

        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)|%(objectname)",
            "refs/heads",
        )
        if refs.returncode == 0:
            for line in refs.stdout.splitlines():
                branch, sep, commit = line.partition("|")
                if not sep or branch in {"main", "master", primary_branch}:
                    continue
                merged = _git(repo, "merge-base", "--is-ancestor", commit, repo_head).returncode == 0
                if not merged:
                    continue
                state = {
                    "repo": str(repo),
                    "branch": branch,
                    "commit": commit,
                    "checked_out": branch in checked_out_branches,
                    "merged_into_repo_head": True,
                }
                items.append(
                    {
                        "resource_kind": "git_branch",
                        "resource_id": f"{repo}:{branch}",
                        "action": "review_branch_cleanup",
                        "safety": "needs_review",
                        "reversible": False,
                        "reason": "local branch is merged; remote and task ownership still require review",
                        "fingerprint": _fingerprint(state),
                        "details": state,
                    }
                )
    return items


def _path_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _artifact_items(repos: Iterable[Path]) -> list[dict[str, Any]]:
    roots: dict[str, tuple[Path, str]] = {}
    hermes_home = get_hermes_home()
    for path, kind in (
        (hermes_home / "logs", "logs"),
        (hermes_home / "checkpoints", "checkpoints"),
        (hermes_home / "kanban" / "logs", "kanban_logs"),
    ):
        roots[str(path)] = (path, kind)
    for repo in repos:
        for name, kind in (
            (".pytest_cache", "test_cache"),
            (".cache", "repo_cache"),
            (".coverage", "test_artifact"),
        ):
            path = repo / name
            roots[str(path)] = (path, kind)

    items: list[dict[str, Any]] = []
    for key in sorted(roots):
        path, kind = roots[key]
        if not path.exists():
            continue
        state = {"path": str(path.resolve()), "kind": kind, "bytes": _path_size(path)}
        items.append(
            {
                "resource_kind": kind,
                "resource_id": state["path"],
                "action": "review_artifact_cleanup",
                "safety": "info",
                "reversible": False,
                "reason": "inventory only; native retention or explicit ownership is required",
                "fingerprint": _fingerprint(state),
                "details": state,
            }
        )
    return items


def scan_plan(
    *,
    boards: Optional[Iterable[str]] = None,
    repos: Optional[Iterable[str]] = None,
    include_repos: bool = True,
    actionable_only: bool = False,
) -> dict[str, Any]:
    _reject_board_overrides()
    board_filter = set(boards) if boards else None
    resolved_repos = _discover_repos(repos) if include_repos else []
    items = _board_items(board_filter)
    if include_repos:
        items.extend(_repo_items(resolved_repos))
        items.extend(_artifact_items(resolved_repos))
    if actionable_only:
        items = [item for item in items if item["safety"] in ACTIONABLE_SAFETY]
    items.sort(key=lambda item: (item["resource_kind"], item["resource_id"], item["action"]))
    safety_counts: dict[str, int] = {}
    for item in items:
        safety = str(item["safety"])
        safety_counts[safety] = safety_counts.get(safety, 0) + 1
    return {
        "contract": PLAN_CONTRACT,
        "summary": {
            "item_count": len(items),
            "safety_counts": dict(sorted(safety_counts.items())),
        },
        "items": items,
    }


def _current_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    kind = item.get("resource_kind")
    rid = str(item.get("resource_id", ""))
    if kind == "kanban_board" and kb.board_exists(rid):
        for candidate in _board_items({rid}):
            if candidate["resource_kind"] == kind and candidate["resource_id"] == rid:
                return candidate
    if kind == "git_worktree_metadata":
        repo = Path(rid)
        for candidate in _repo_items([repo]):
            if candidate["resource_kind"] == kind and candidate["resource_id"] == rid:
                return candidate
    return None


def _gateway_pid() -> Optional[int]:
    try:
        from gateway.status import resolve_gateway_liveness

        state = resolve_gateway_liveness(
            profile_dir=get_hermes_home(), use_cache=False
        )
        if state.probe_error:
            raise RuntimeError(f"gateway liveness probe failed: {state.probe_error}")
        return state.pid
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"cannot prove gateway is stopped: {exc}") from exc


@contextmanager
def _board_maintenance(slug: str):
    marker = kb.board_maintenance_marker(slug)
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not kb.clear_stale_board_maintenance_marker(slug):
        raise RuntimeError(f"board {slug!r} is already under maintenance")
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"board {slug!r} is already under maintenance") from exc
    lifecycle_handle = None
    try:
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        finally:
            os.close(fd)
        lifecycle_handle = kb._acquire_board_lifecycle_handle(
            _explicit_board_db_path(slug), exclusive=True
        )
        yield
    finally:
        marker.unlink(missing_ok=True)
        kb._release_board_lifecycle_handle(lifecycle_handle)


def _write_tombstone(path: Path, text: str, *, create: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if create else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _apply_archive_board(item: dict[str, Any]) -> dict[str, Any]:
    slug = str(item["resource_id"])
    with _board_maintenance(slug):
        current = _current_item(item)
        if current is None:
            return {"resource_id": slug, "action": "archive_board", "status": "already_absent"}
        if current.get("fingerprint") != item.get("fingerprint") or current.get("action") != "archive_board":
            return {"resource_id": slug, "action": "archive_board", "status": "rejected", "error": "state fingerprint changed; rescan required"}
        tombstone = kb.board_archive_tombstone(slug)
        _write_tombstone(tombstone, f"pending:{os.getpid()}\n", create=True)
        try:
            result = kb.remove_board(slug, archive=True)
            _write_tombstone(
                tombstone, f"archived:{result['new_path']}\n", create=False
            )
        except Exception:
            if kb.board_dir(slug).exists():
                tombstone.unlink(missing_ok=True)
            raise
        return {"resource_id": slug, "action": "archive_board", "status": "applied", "archive_path": result["new_path"]}


def apply_plan(plan: dict[str, Any], *, allow_nonrecoverable: bool = False) -> dict[str, Any]:
    _reject_board_overrides()
    if plan.get("contract") != PLAN_CONTRACT or not isinstance(plan.get("items"), list):
        raise ValueError(f"plan must use contract {PLAN_CONTRACT}")
    mutating = [
        item for item in plan["items"]
        if item.get("action") in {"archive_board", "prune_worktree_metadata"}
    ]
    if len(mutating) > 1:
        raise ValueError("apply accepts exactly one mutating plan row; rescan a narrower scope")
    if any(item.get("action") == "archive_board" for item in mutating) and _gateway_pid():
        raise ValueError("archive apply requires the Hermes gateway to be stopped")

    results: list[dict[str, Any]] = []
    preflight: dict[int, Optional[dict[str, Any]]] = {}
    preflight_error: Optional[str] = None
    for index, item in enumerate(plan["items"]):
        action = item.get("action")
        if action not in {"archive_board", "prune_worktree_metadata"}:
            continue
        if item.get("safety") != "safe":
            preflight_error = "item is not policy-safe"
            break
        if not item.get("reversible") and not allow_nonrecoverable:
            preflight_error = "nonrecoverable action requires --allow-nonrecoverable"
            break
        current = _current_item(item)
        preflight[index] = current
        if current is None:
            continue
        if current.get("fingerprint") != item.get("fingerprint") or current.get("action") != action:
            preflight_error = "state fingerprint changed; rescan required"
            break
    if preflight_error:
        return {
            "contract": "hermes.janitor.apply.v1",
            "results": [{"resource_id": item.get("resource_id"), "action": item.get("action"), "status": "rejected", "error": preflight_error}],
            "summary": {"rejected": 1},
        }

    for index, item in enumerate(plan["items"]):
        action = item.get("action")
        if action not in {"archive_board", "prune_worktree_metadata"}:
            results.append({"resource_id": item.get("resource_id"), "action": action, "status": "skipped"})
            continue
        if preflight.get(index) is None:
            results.append({"resource_id": item.get("resource_id"), "action": action, "status": "already_absent"})
            continue
        if action == "archive_board":
            results.append(_apply_archive_board(item))
        else:
            repo = Path(str(item["resource_id"]))
            proc = _git(repo, "worktree", "prune", "--expire", "now", "--verbose")
            results.append({"resource_id": str(repo), "action": action, "status": "applied" if proc.returncode == 0 else "failed", "error": proc.stderr.strip() if proc.returncode else ""})
    return {
        "contract": "hermes.janitor.apply.v1",
        "results": results,
        "summary": {
            status: sum(1 for result in results if result["status"] == status)
            for status in sorted({result["status"] for result in results})
        },
    }


def restore_board(archive_path: str, slug: str) -> dict[str, str]:
    _reject_board_overrides()
    if _gateway_pid():
        raise ValueError("board restore requires the Hermes gateway to be stopped")
    normed = kb._normalize_board_slug(slug)
    if not normed or normed == kb.DEFAULT_BOARD:
        raise ValueError("a non-default board slug is required")
    source = Path(archive_path).expanduser().resolve()
    archive_root = (kb.boards_root() / "_archived").resolve()
    if source.parent != archive_root:
        raise ValueError("archive path must be a direct child of the Hermes board archive root")
    if not source.name.startswith(f"{normed}-"):
        raise ValueError("archive directory name does not match the requested board slug")
    if not source.is_dir() or not ((source / "kanban.db").exists() or (source / "board.json").exists()):
        raise ValueError("archive path is not a recoverable Kanban board")
    target = kb.board_dir(normed)
    with _board_maintenance(normed):
        if target.exists():
            raise ValueError(f"board {normed!r} already exists")
        source.rename(target)
        kb.board_archive_tombstone(normed).unlink(missing_ok=True)
    return {"slug": normed, "action": "restored", "path": str(target)}


def build_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "janitor",
        help="Scan and apply fingerprinted, policy-bounded cleanup plans",
        description="Deterministic cleanup inventory. Scans never mutate state.",
    )
    actions = parser.add_subparsers(dest="janitor_action")

    scan = actions.add_parser("scan", help="Create a deterministic read-only cleanup plan")
    scan.add_argument("--board", action="append", dest="boards", help="Limit to a board slug; repeatable")
    scan.add_argument("--repo", action="append", dest="repos", help="Limit repository inventory to this path; repeatable")
    scan.add_argument("--no-repos", action="store_true", help="Skip repositories, branches, worktrees, logs, and cache inventory")
    scan.add_argument("--actionable-only", action="store_true", help="Omit informational and must-preserve rows")
    scan.add_argument("--plan", help="Write the exact JSON plan to this file")
    scan.add_argument("--json", action="store_true", help="Print the full JSON plan")

    apply_cmd = actions.add_parser("apply", help="Apply policy-safe rows from a saved plan")
    apply_cmd.add_argument("--plan", required=True, help="Plan file created by janitor scan")
    apply_cmd.add_argument("--yes", action="store_true", help="Confirm plan application")
    apply_cmd.add_argument("--allow-nonrecoverable", action="store_true", help="Permit policy-safe metadata pruning; never implied")

    restore = actions.add_parser("restore-board", help="Restore a recoverably archived board")
    restore.add_argument("archive_path", help="Archived board directory")
    restore.add_argument("--slug", required=True, help="Board slug to restore")
    restore.add_argument("--yes", action="store_true", help="Confirm restoration")

    parser.set_defaults(func=cmd_janitor)
    return parser


def cmd_janitor(args: argparse.Namespace) -> int:
    action = getattr(args, "janitor_action", None)
    if action == "scan":
        plan = scan_plan(
            boards=args.boards,
            repos=args.repos,
            include_repos=not args.no_repos,
            actionable_only=args.actionable_only,
        )
        encoded = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if args.plan:
            path = Path(args.plan).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")
        if args.json:
            print(encoded, end="")
        else:
            print(f"Janitor scan: {plan['summary']['item_count']} item(s)")
            for safety, count in plan["summary"]["safety_counts"].items():
                print(f"  {safety}: {count}")
            if args.plan:
                print(f"Plan written: {Path(args.plan).expanduser()}")
        return 0

    if action == "apply":
        if not args.yes:
            print("janitor apply requires --yes", file=sys.stderr)
            return 2
        try:
            plan = json.loads(Path(args.plan).expanduser().read_text(encoding="utf-8"))
            result = apply_plan(plan, allow_nonrecoverable=args.allow_nonrecoverable)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"janitor apply: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if result["summary"].get("failed") or result["summary"].get("rejected") else 0

    if action == "restore-board":
        if not args.yes:
            print("janitor restore-board requires --yes", file=sys.stderr)
            return 2
        try:
            result = restore_board(args.archive_path, args.slug)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"janitor restore-board: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print("usage: hermes janitor {scan,apply,restore-board} ...", file=sys.stderr)
    return 2
