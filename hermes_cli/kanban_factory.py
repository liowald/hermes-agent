"""Durable multi-role coding factory built on ordinary Kanban cards.

The human-facing root card is never a worker card.  Separate phase cards own
implementation, two independent reviews, repairs, and final delivery.  A
SQLite trigger keeps the root from becoming ``done`` until the deterministic
reconciler has validated every phase receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.profiles import profile_exists, validate_profile_name


FACTORY_VERSION = 2
WORK_ROOT_TEMPLATE = kb.GUARDED_WORK_ROOT_TEMPLATE
_WORK_ROOT_KEY_PREFIX = "hermes-factory-root:"
_TERMINAL = {"done", "archived"}
_OPEN = {"ready", "running", "todo", "blocked", "review", "scheduled", "triage"}


class _ExistingFactory(Exception):
    def __init__(self, root_id: str):
        self.root_id = root_id


def ensure_schema(conn) -> None:
    """Install the additive factory schema and the downgrade-safe root guard."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS factory_workflows (
            root_id             TEXT PRIMARY KEY,
            request_key         TEXT,
            contract_version    INTEGER NOT NULL,
            state               TEXT NOT NULL,
            cycle               INTEGER NOT NULL DEFAULT 0,
            executor_profile    TEXT NOT NULL,
            reviewer_a_profile  TEXT NOT NULL,
            reviewer_b_profile  TEXT NOT NULL,
            fixer_profile       TEXT NOT NULL,
            delivery_mode       TEXT NOT NULL DEFAULT 'draft_pr',
            delivery_repo       TEXT,
            delivery_base       TEXT,
            delivery_base_sha   TEXT,
            implement_task_id   TEXT NOT NULL,
            reviewer_a_task_id  TEXT,
            reviewer_b_task_id  TEXT,
            fixer_task_id       TEXT,
            delivery_task_id    TEXT,
            candidate_sha       TEXT,
            review_bundle_path  TEXT,
            plan_snapshot       TEXT,
            plan_sha256         TEXT,
            final_receipt       TEXT,
            last_error          TEXT,
            blocked_from_state  TEXT,
            retry_attempt       INTEGER NOT NULL DEFAULT 0,
            created_at          INTEGER NOT NULL,
            updated_at          INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_factory_state
            ON factory_workflows(state, updated_at);
        CREATE TRIGGER IF NOT EXISTS factory_root_done_guard
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status = 'done'
          AND EXISTS (
              SELECT 1 FROM factory_workflows f
               WHERE f.root_id = NEW.id
                 AND f.state NOT IN ('completing', 'done')
          )
        BEGIN
            SELECT RAISE(ABORT, 'factory root is not verified');
        END;
        CREATE TRIGGER IF NOT EXISTS factory_root_status_guard
        BEFORE UPDATE OF status ON tasks
        WHEN NEW.status != OLD.status
          AND EXISTS (
              SELECT 1 FROM factory_workflows f
               WHERE f.root_id = NEW.id
                 AND f.state != 'done'
                 AND NOT (f.state = 'completing' AND NEW.status = 'done')
          )
        BEGIN
            SELECT RAISE(ABORT, 'factory root is not verified; status is controller-owned');
        END;
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(factory_workflows)")}
    if "request_key" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN request_key TEXT")
    if "final_receipt" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN final_receipt TEXT")
    if "plan_snapshot" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN plan_snapshot TEXT")
    if "plan_sha256" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN plan_sha256 TEXT")
    if "delivery_mode" not in columns:
        conn.execute(
            "ALTER TABLE factory_workflows ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'draft_pr'"
        )
    if "delivery_repo" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN delivery_repo TEXT")
    if "delivery_base" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN delivery_base TEXT")
    if "delivery_base_sha" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN delivery_base_sha TEXT")
    if "blocked_from_state" not in columns:
        conn.execute("ALTER TABLE factory_workflows ADD COLUMN blocked_from_state TEXT")
    if "retry_attempt" not in columns:
        conn.execute(
            "ALTER TABLE factory_workflows ADD COLUMN retry_attempt INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_factory_request_key "
        "ON factory_workflows(request_key) WHERE request_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_guarded_work_root_key "
        "ON tasks(idempotency_key) "
        f"WHERE workflow_template_id='{WORK_ROOT_TEMPLATE}' "
        "AND idempotency_key IS NOT NULL"
    )


def _profile(name: str, role: str) -> str:
    value = str(name or "").strip().lower()
    validate_profile_name(value)
    if not profile_exists(value):
        raise ValueError(f"{role} profile {value!r} is not installed")
    return value


def _workspace_delivery_target(path: Path) -> tuple[str, str, str]:
    """Return the GitHub repository, default branch, and local base ref SHA."""
    origin_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    repo = _github_repo_slug(origin_url)
    if not repo:
        raise ValueError("workspace origin is not a GitHub repository")
    origin_head = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    prefix = "origin/"
    if not origin_head.startswith(prefix) or len(origin_head) == len(prefix):
        raise ValueError("workspace origin default branch is unavailable")
    base = origin_head[len(prefix):]
    base_sha = subprocess.run(
        ["git", "rev-parse", f"refs/remotes/origin/{base}"],
        cwd=path, text=True, capture_output=True, check=True, timeout=15,
    ).stdout.strip()
    return repo, base, base_sha


def _require_clean_intake_base(path: Path, expected_sha: str) -> None:
    """Reject a factory seeded from a dirty or non-default checkout."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True,
        capture_output=True, check=True, timeout=15,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=path, text=True,
        capture_output=True, check=True, timeout=30,
    ).stdout.strip()
    if head != expected_sha or status:
        raise ValueError(
            "factory intake requires a clean checkout at the exact selected "
            "base commit"
        )


def _workspace_head_sha(path: Path) -> str:
    """Return the checked-out commit used as the immutable local delivery base."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True,
        capture_output=True, check=True, timeout=15,
    ).stdout.strip()


def _factory_source_repo(
    workspace_path: Optional[str], project_id: Optional[str],
) -> Optional[Path]:
    if workspace_path:
        path = Path(workspace_path).expanduser().resolve()
        if path.is_dir():
            return path
    if project_id:
        from hermes_cli import projects_db

        with projects_db.connect_closing() as project_conn:
            project = projects_db.get_project(project_conn, str(project_id))
        if project and project.primary_path:
            return Path(project.primary_path).expanduser().resolve()
    return None


def _root_key(request_key: str) -> str:
    return f"{_WORK_ROOT_KEY_PREFIX}{request_key}"


def _request_key_from_root(task: kb.Task) -> str:
    value = str(task.idempotency_key or "")
    if not value.startswith(_WORK_ROOT_KEY_PREFIX):
        raise ValueError("managed work root has no factory request key")
    request_key = value[len(_WORK_ROOT_KEY_PREFIX):].strip()
    if not request_key:
        raise ValueError("managed work root has an empty factory request key")
    return request_key


def _plan_contract(task: kb.Task) -> tuple[str, str]:
    payload = {
        "contract": "hermes.work-root.plan.v1",
        "root_id": task.id,
        "title": task.title,
        "body": task.body or "",
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stage(conn, root_id: str, value: str) -> None:
    conn.execute(
        "UPDATE tasks SET current_step_key=? WHERE id=?",
        (value, root_id),
    )


def _plan_root_for_adoption(
    conn,
    root: kb.Task,
    *,
    title: Optional[str],
    body: Optional[str],
) -> kb.Task:
    """Persist an approved plan without exposing a dispatchable state."""
    if body is None:
        return root
    planned_body = str(body).strip()
    if not planned_body:
        raise ValueError("approved plan body cannot be blank")
    planned_title = str(title).strip() if title is not None else root.title
    if not planned_title:
        raise ValueError("approved plan title cannot be blank")
    if root.status != "triage" or root.current_step_key not in {"intake", "planned"}:
        raise ValueError("only an intake or planned Work Root can accept a plan")
    changed_fields = [
        field
        for field, before, after in (
            ("title", root.title, planned_title),
            ("body", root.body or "", planned_body),
        )
        if before != after
    ]
    conn.execute(
        "UPDATE tasks SET title=?,body=?,assignee=NULL,current_step_key='planned' "
        "WHERE id=? AND status='triage'",
        (planned_title, planned_body, root.id),
    )
    kb._append_event(
        conn,
        root.id,
        "specified",
        {
            "changed_fields": changed_fields,
            "held_for_factory": True,
            "current_step_key": "planned",
        },
    )
    planned = kb.get_task(conn, root.id)
    if planned is None:
        raise RuntimeError("planned Work Root did not persist")
    return planned


def inspect_work_root(conn, root_id: str) -> dict[str, Any]:
    """Return one truthful receipt before or after guarded factory adoption."""
    ensure_schema(conn)
    task = kb.get_task(conn, root_id)
    if task is None:
        raise ValueError(f"work root {root_id!r} not found")
    workflow = conn.execute(
        "SELECT 1 FROM factory_workflows WHERE root_id=?", (root_id,)
    ).fetchone()
    if workflow:
        return inspect_factory(conn, root_id)
    if task.workflow_template_id != WORK_ROOT_TEMPLATE:
        raise ValueError(f"managed work root {root_id!r} not found")
    return {
        "root_id": root_id,
        "state": task.current_step_key or "intake",
        "contract_version": FACTORY_VERSION,
        "workflow_template_id": task.workflow_template_id,
        "root_status": task.status,
        "implement_task_id": None,
        "candidate_sha": None,
        "plan_sha256": None,
    }


def find_work_root_for_origin(
    conn,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve the newest managed root subscribed to one exact origin."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT t.id FROM kanban_notify_subs s JOIN tasks t ON t.id=s.task_id "
        "LEFT JOIN factory_workflows f ON f.root_id=t.id "
        "WHERE lower(s.platform)=lower(?) AND s.chat_id=? AND s.thread_id=? "
        "AND (t.workflow_template_id=? OR f.root_id IS NOT NULL) "
        "ORDER BY s.created_at DESC,t.created_at DESC LIMIT 1",
        (platform, chat_id, thread_id or "", WORK_ROOT_TEMPLATE),
    ).fetchone()
    return str(row["id"]) if row else None


def work_root_matches_request_key(
    conn, root_id: str, request_key: str,
) -> bool:
    """Return whether a held Work Root is owned by one canonical request key."""
    row = conn.execute(
        "SELECT idempotency_key,workflow_template_id FROM tasks WHERE id=?",
        (root_id,),
    ).fetchone()
    return bool(
        row
        and row["workflow_template_id"] == WORK_ROOT_TEMPLATE
        and row["idempotency_key"] == _root_key(request_key)
    )


def create_work_root(
    conn,
    *,
    title: str,
    body: str = "",
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None,
    priority: int = 0,
    tenant: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    created_by: str = "factory-controller",
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create an idempotent, non-dispatchable Work Root in intake."""
    ensure_schema(conn)
    request_key = str(idempotency_key or "").strip()
    if not request_key:
        raise ValueError("idempotency_key is required for work-root intake")
    if not str(title or "").strip():
        raise ValueError("title is required for work-root intake")
    root_key = _root_key(request_key)
    existing = conn.execute(
        "SELECT id,workflow_template_id FROM tasks WHERE idempotency_key=?",
        (root_key,),
    ).fetchone()
    if existing:
        if existing["workflow_template_id"] != WORK_ROOT_TEMPLATE:
            raise ValueError(
                "reserved work-root idempotency key belongs to an unmanaged task"
            )
        return inspect_work_root(conn, existing["id"])

    try:
        with kb.write_txn(conn):
            existing = conn.execute(
                "SELECT id,workflow_template_id FROM tasks WHERE idempotency_key=?",
                (root_key,),
            ).fetchone()
            if existing:
                if existing["workflow_template_id"] != WORK_ROOT_TEMPLATE:
                    raise ValueError(
                        "reserved work-root idempotency key belongs to an unmanaged task"
                    )
                raise _ExistingFactory(existing["id"])
            root_id = kb.create_task(
                conn,
                title=title,
                body=body,
                assignee=None,
                priority=priority,
                tenant=tenant,
                idempotency_key=root_key,
                created_by=created_by,
                triage=True,
                workspace_kind="dir",
                workspace_path=workspace_path,
                project_id=project_id,
                session_id=session_id,
                workflow_template_id=WORK_ROOT_TEMPLATE,
                current_step_key="intake",
            )
            kb._append_event(
                conn,
                root_id,
                "work_root_created",
                {
                    "contract_version": FACTORY_VERSION,
                    "workflow_template_id": WORK_ROOT_TEMPLATE,
                },
            )
    except _ExistingFactory as existing_root:
        return inspect_work_root(conn, existing_root.root_id)
    return inspect_work_root(conn, root_id)


def _factory_preflight(
    *,
    workspace_path: Optional[str],
    project_id: Optional[str],
    executor: str,
    reviewer_a: str,
    reviewer_b: str,
    fixer: str,
    delivery_mode: str,
) -> dict[str, Any]:
    executor = _profile(executor, "executor")
    reviewer_a = _profile(reviewer_a, "reviewer-a")
    reviewer_b = _profile(reviewer_b, "reviewer-b")
    fixer = _profile(fixer, "fixer")
    if len({executor, reviewer_a, reviewer_b, fixer}) != 4:
        raise ValueError("executor, reviewer-a, reviewer-b, and fixer must be distinct profiles")
    mode = str(delivery_mode or "").strip().lower()
    if mode not in {"draft_pr", "local_commit"}:
        raise ValueError("delivery_mode must be draft_pr or local_commit")
    source_repo = _factory_source_repo(workspace_path, project_id)
    if source_repo is None:
        raise ValueError(f"{mode} factories require a repository workspace or project")
    delivery_repo = None
    delivery_base = None
    if mode == "draft_pr":
        delivery_repo, delivery_base, delivery_base_sha = _workspace_delivery_target(source_repo)
    else:
        delivery_base_sha = _workspace_head_sha(source_repo)
    _require_clean_intake_base(source_repo, delivery_base_sha)
    return {
        "executor": executor,
        "reviewer_a": reviewer_a,
        "reviewer_b": reviewer_b,
        "fixer": fixer,
        "delivery_mode": mode,
        "delivery_repo": delivery_repo,
        "delivery_base": delivery_base,
        "delivery_base_sha": delivery_base_sha,
    }


def _insert_factory_for_root(
    conn,
    *,
    root: kb.Task,
    request_key: str,
    workspace_kind: str,
    workspace_path: Optional[str],
    project_id: Optional[str],
    settings: dict[str, Any],
    priority: int,
    tenant: Optional[str],
) -> str:
    """Insert the implementation phase and workflow inside an outer txn."""
    if root.workflow_template_id != WORK_ROOT_TEMPLATE:
        raise ValueError("factory adoption requires a managed work root")
    if root.status != "triage" or root.current_step_key != "planned":
        raise ValueError("work root must be specified and held in planned triage")
    if not str(root.body or "").strip():
        raise ValueError("work root approved plan body cannot be blank")
    if root.current_run_id or root.claim_lock:
        raise ValueError("work root has an active run or claim")
    linked = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id=? OR child_id=? LIMIT 1",
        (root.id, root.id),
    ).fetchone()
    if linked:
        raise ValueError("work root cannot be adopted after generic task linking")
    existing_workflow = conn.execute(
        "SELECT implement_task_id FROM factory_workflows WHERE root_id=?",
        (root.id,),
    ).fetchone()
    if existing_workflow:
        return str(existing_workflow["implement_task_id"])

    phase_key = f"hermes-factory-phase:{request_key}:implement"
    collision = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key=? LIMIT 1", (phase_key,)
    ).fetchone()
    if collision:
        raise ValueError(
            "reserved factory phase key belongs to an unrelated task; "
            f"refusing to attach workflow to {collision['id']}"
        )
    plan_snapshot, plan_sha256 = _plan_contract(root)
    implement_body = (
        f"Factory root: {root.id}\nApproved plan SHA-256: {plan_sha256}\n"
        f"Approved plan snapshot: {plan_snapshot}\n\n"
        "Implement the approved plan in the isolated workspace. Do not review "
        "your own work. Run the relevant local gates. Do not call request-review: "
        "the factory creates separate reviewer cards. Call complete with metadata "
        "containing non-empty changed_files and tests_run, inspection_only=false, "
        "and delivery evidence if already available."
    )
    implement_id = kb.create_task(
        conn,
        title=f"Implement: {root.title}",
        body=implement_body,
        assignee=settings["executor"],
        priority=priority,
        tenant=tenant,
        created_by=root.id,
        initial_status="running",
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        project_id=project_id,
        idempotency_key=phase_key,
        goal_mode=False,
    )
    implement_task = kb.get_task(conn, implement_id)
    if implement_task and implement_task.project_id:
        conn.execute(
            "UPDATE tasks SET project_id=? WHERE id=?",
            (implement_task.project_id, root.id),
        )
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status='blocked',assignee=NULL,current_step_key='implementing' "
        "WHERE id=? AND status='triage'",
        (root.id,),
    )
    conn.execute(
        "INSERT INTO factory_workflows "
        "(root_id,request_key,contract_version,state,cycle,executor_profile,reviewer_a_profile,"
        "reviewer_b_profile,fixer_profile,delivery_mode,delivery_repo,delivery_base,delivery_base_sha,"
        "implement_task_id,plan_snapshot,plan_sha256,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            root.id, request_key, FACTORY_VERSION, "implementing", 0,
            settings["executor"], settings["reviewer_a"], settings["reviewer_b"],
            settings["fixer"], settings["delivery_mode"], settings["delivery_repo"],
            settings["delivery_base"], settings["delivery_base_sha"], implement_id,
            plan_snapshot, plan_sha256, now, now,
        ),
    )
    kb._append_event(
        conn,
        root.id,
        "factory_created",
        {
            "contract_version": FACTORY_VERSION,
            "implement_task_id": implement_id,
            "plan_sha256": plan_sha256,
            "roles": {
                "executor": settings["executor"],
                "reviewer_a": settings["reviewer_a"],
                "reviewer_b": settings["reviewer_b"],
                "fixer": settings["fixer"],
            },
        },
    )
    return implement_id


def start_factory_from_root(
    conn,
    root_id: str,
    *,
    workspace_kind: str = "worktree",
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None,
    executor: str = "executor",
    reviewer_a: str = "reviewer-a",
    reviewer_b: str = "reviewer-b",
    fixer: str = "fixer",
    priority: Optional[int] = None,
    tenant: Optional[str] = None,
    delivery_mode: str = "draft_pr",
    plan_title: Optional[str] = None,
    plan_body: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically plan, when supplied, and adopt the same guarded Work Root."""
    ensure_schema(conn)
    existing = conn.execute(
        "SELECT 1 FROM factory_workflows WHERE root_id=?", (root_id,)
    ).fetchone()
    if existing:
        return inspect_factory(conn, root_id)
    root = kb.get_task(conn, root_id)
    if root is None or root.workflow_template_id != WORK_ROOT_TEMPLATE:
        raise ValueError(f"managed work root {root_id!r} not found")
    request_key = _request_key_from_root(root)
    effective_workspace = workspace_path or root.workspace_path
    effective_project = project_id or root.project_id
    settings = _factory_preflight(
        workspace_path=effective_workspace,
        project_id=effective_project,
        executor=executor,
        reviewer_a=reviewer_a,
        reviewer_b=reviewer_b,
        fixer=fixer,
        delivery_mode=delivery_mode,
    )
    with kb.write_txn(conn):
        adopted = conn.execute(
            "SELECT 1 FROM factory_workflows WHERE root_id=?", (root_id,)
        ).fetchone()
        if not adopted:
            owner = conn.execute(
                "SELECT root_id FROM factory_workflows WHERE request_key=?",
                (request_key,),
            ).fetchone()
            if owner:
                raise ValueError(
                    f"factory request key already belongs to root {owner['root_id']}"
                )
            current = kb.get_task(conn, root_id)
            if current is None:
                raise ValueError(f"managed work root {root_id!r} not found")
            current = _plan_root_for_adoption(
                conn,
                current,
                title=plan_title,
                body=plan_body,
            )
            _insert_factory_for_root(
                conn,
                root=current,
                request_key=request_key,
                workspace_kind=workspace_kind,
                workspace_path=effective_workspace,
                project_id=effective_project,
                settings=settings,
                priority=current.priority if priority is None else int(priority),
                tenant=current.tenant if tenant is None else tenant,
            )
    return inspect_factory(conn, root_id)


def create_factory(
    conn,
    *,
    title: str,
    body: str,
    workspace_kind: str = "worktree",
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None,
    executor: str = "executor",
    reviewer_a: str = "reviewer-a",
    reviewer_b: str = "reviewer-b",
    fixer: str = "fixer",
    priority: int = 0,
    tenant: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    created_by: str = "factory-controller",
    delivery_mode: str = "draft_pr",
) -> dict[str, Any]:
    """Create a planned Work Root and atomically start its guarded factory.

    This preserves the original one-call contract. New intake-first callers
    use :func:`create_work_root` followed by :func:`start_factory_from_root`.
    """
    ensure_schema(conn)
    request_key = str(idempotency_key or "").strip()
    if not request_key:
        raise ValueError("idempotency_key is required for factory creation")
    existing = conn.execute(
        "SELECT root_id FROM factory_workflows WHERE request_key=?", (request_key,)
    ).fetchone()
    if existing:
        return inspect_factory(conn, existing["root_id"])
    if workspace_kind not in {"worktree", "dir"}:
        raise ValueError("workspace_kind must be worktree or dir")
    settings = _factory_preflight(
        workspace_path=workspace_path,
        project_id=project_id,
        executor=executor,
        reviewer_a=reviewer_a,
        reviewer_b=reviewer_b,
        fixer=fixer,
        delivery_mode=delivery_mode,
    )
    root_key = _root_key(request_key)
    phase_key = f"hermes-factory-phase:{request_key}:implement"
    collision = conn.execute(
        "SELECT id,workflow_template_id FROM tasks WHERE idempotency_key IN (?,?) LIMIT 1",
        (root_key, phase_key),
    ).fetchone()
    if collision:
        raise ValueError(
            "reserved factory idempotency key already belongs to an unguarded task; "
            f"refusing to attach workflow to {collision['id']}"
        )
    try:
        with kb.write_txn(conn):
            existing = conn.execute(
                "SELECT root_id FROM factory_workflows WHERE request_key=?",
                (request_key,),
            ).fetchone()
            if existing:
                raise _ExistingFactory(existing["root_id"])
            collision = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key IN (?,?) LIMIT 1",
                (root_key, phase_key),
            ).fetchone()
            if collision:
                raise ValueError(
                    "reserved factory idempotency key already belongs to an unguarded task; "
                    f"refusing to attach workflow to {collision['id']}"
                )
            root_id = kb.create_task(
                conn,
                title=title,
                body=body,
                assignee=None,
                priority=priority,
                tenant=tenant,
                idempotency_key=root_key,
                created_by=created_by,
                triage=True,
                workspace_kind="dir",
                workspace_path=workspace_path,
                project_id=None,
                workflow_template_id=WORK_ROOT_TEMPLATE,
                current_step_key="planned",
            )
            root = kb.get_task(conn, root_id)
            if root is None:
                raise RuntimeError("factory root creation did not persist")
            _insert_factory_for_root(
                conn,
                root=root,
                request_key=request_key,
                workspace_kind=workspace_kind,
                workspace_path=workspace_path,
                project_id=project_id,
                settings=settings,
                priority=priority,
                tenant=tenant,
            )
    except _ExistingFactory as existing_factory:
        return inspect_factory(conn, existing_factory.root_id)
    return inspect_factory(conn, root_id)


def inspect_factory(conn, root_id: str) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"factory root {root_id!r} not found")
    result = dict(row)
    for key in (
        "root_id", "implement_task_id", "reviewer_a_task_id",
        "reviewer_b_task_id", "fixer_task_id", "delivery_task_id",
    ):
        tid = result.get(key)
        if tid:
            task = kb.get_task(conn, tid)
            result[f"{key[:-3]}status"] = task.status if task else "missing"
    return result


def _phase_receipt(conn, task_id: str, expected_profile: str) -> tuple[Optional[dict], Optional[str]]:
    task = kb.get_task(conn, task_id)
    if task is None:
        return None, "phase task is missing"
    if task.status in {"blocked", "scheduled", "triage"}:
        return None, (
            f"phase {task_id} entered {task.status}"
            + (f": {task.last_failure_error}" if task.last_failure_error else "")
        )
    if task.status not in _TERMINAL:
        return None, None
    run = kb.latest_run(conn, task_id)
    if run is None or run.outcome != "completed":
        return None, "phase has no completed run receipt"
    if run.profile != expected_profile:
        return None, f"phase receipt profile {run.profile!r} is not {expected_profile!r}"
    if not isinstance(run.metadata, dict):
        return None, "phase receipt metadata is missing"
    return dict(run.metadata), None


def _workspace_tree_sha(path: Path) -> str:
    """Return the exact Git tree for tracked and non-ignored workspace bytes.

    A temporary index preserves executable bits, symlinks, deletions, and
    submodule gitlinks without changing the user's real index. ``git add`` may
    write content-addressed objects, but never changes refs or workspace state.
    """
    fd, index_name = tempfile.mkstemp(prefix="hermes-factory-index-")
    os.close(fd)
    try:
        os.unlink(index_name)  # Git requires a missing or valid index, not an empty file.
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = index_name
        subprocess.run(
            ["git", "read-tree", "HEAD"], cwd=path, env=env,
            capture_output=True, check=True, timeout=30,
        )
        subprocess.run(
            ["git", "add", "-A", "--", "."], cwd=path, env=env,
            capture_output=True, check=True, timeout=60,
        )
        return subprocess.run(
            ["git", "write-tree"], cwd=path, env=env, text=True,
            capture_output=True, check=True, timeout=30,
        ).stdout.strip()
    finally:
        try:
            os.unlink(index_name)
        except FileNotFoundError:
            pass


def _candidate_bundle(
    workspace: str,
    root_id: str,
    cycle: int,
    base_sha: Optional[str] = None,
    plan_snapshot: Optional[str] = None,
    plan_sha256: Optional[str] = None,
) -> tuple[str, str, list[str]]:
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"implementation workspace is unavailable: {path}")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=path, text=True,
        capture_output=True, check=True, timeout=30,
    ).stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True,
        capture_output=True, check=True, timeout=30,
    ).stdout.strip()
    diff_base = base_sha or "HEAD"
    if base_sha:
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha, "HEAD"],
            cwd=path, capture_output=True, timeout=30,
        )
        if ancestry.returncode != 0:
            raise ValueError("workspace HEAD is not descended from the factory intake base")
    diff = subprocess.run(
        ["git", "diff", "--binary", diff_base], cwd=path, text=True,
        capture_output=True, check=True, timeout=60,
    ).stdout
    staged = subprocess.run(
        ["git", "diff", "--binary", "--cached", "HEAD"], cwd=path, text=True,
        capture_output=True, check=True, timeout=60,
    ).stdout
    payload = {
        "contract": "hermes.factory.review-bundle.v1",
        "root_id": root_id,
        "cycle": cycle,
        "workspace": str(path),
        "head": head,
        "base_sha": base_sha,
        "plan_snapshot": plan_snapshot,
        "plan_sha256": plan_sha256,
        "status": status.splitlines(),
        "diff": diff,
        "staged_diff": staged,
    }
    candidate = _workspace_tree_sha(path)
    payload["candidate_sha"] = candidate
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    out = Path.home() / ".hermes" / "factory" / "reviews" / root_id
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle = out / f"cycle-{cycle}-{candidate[:12]}.json"
    bundle.write_bytes(encoded)
    os.chmod(bundle, 0o600)
    changed = subprocess.run(
        ["git", "diff", "--name-only", diff_base], cwd=path, text=True,
        capture_output=True, check=True, timeout=30,
    ).stdout.splitlines()
    changed.extend(line[3:] for line in status.splitlines() if len(line) > 3)
    changed = list(dict.fromkeys(changed))
    return candidate, str(bundle), changed


def _create_review_task(conn, row, *, profile: str, label: str, candidate: str, bundle: str) -> str:
    root = kb.get_task(conn, row["root_id"])
    implement = kb.get_task(conn, row["implement_task_id"])
    body = (
        f"Factory root: {row['root_id']}\nApproved plan SHA-256: {row['plan_sha256'] or 'legacy-unbound'}\n"
        f"Approved plan snapshot: {row['plan_snapshot'] or '(legacy root body)'}\n"
        f"Candidate Git tree: {candidate}\n"
        f"Review bundle: {bundle}\nWorkspace: {implement.workspace_path if implement else ''}\n\n"
        "Independently review the exact bundled candidate against the frozen plan "
        "criteria. You are report-only: do not modify repository files. Complete with "
        "metadata {verdict: approve|changes, candidate_sha: <exact>, findings: [...], "
        "verification: [...]}."
    )
    return kb.create_task(
        conn,
        title=f"Review {label}: {root.title if root else row['root_id']}",
        body=body,
        assignee=profile,
        created_by=row["root_id"],
        initial_status="running",
        workspace_kind="dir",
        workspace_path=implement.workspace_path if implement else None,
        tenant=root.tenant if root else None,
        project_id=root.project_id if root else None,
        worker_toolsets=["factory_review_readonly"],
        idempotency_key=f"factory:{row['root_id']}:cycle:{row['cycle']}:{label}",
        goal_mode=False,
    )


def _set_error(conn, root_id: str, message: str) -> dict[str, Any]:
    now = int(time.time())
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE factory_workflows SET blocked_from_state=CASE WHEN state!='blocked' THEN state ELSE blocked_from_state END,"
            "state='blocked',last_error=?,updated_at=? WHERE root_id=? AND state!='done'",
            (message[:2000], now, root_id),
        )
        if cur.rowcount:
            _stage(conn, root_id, "blocked")
            kb._append_event(conn, root_id, "factory_blocked", {"reason": message[:2000]})
    return inspect_factory(conn, root_id)


def retry_factory(conn, root_id: str) -> dict[str, Any]:
    """Retry a blocked phase while preserving its role and workspace ownership."""
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)).fetchone()
    if row is None:
        raise ValueError(f"factory root {root_id!r} not found")
    if row["state"] != "blocked" or not row["blocked_from_state"]:
        raise ValueError("factory is not in a recoverable blocked state")
    prior = row["blocked_from_state"]
    phase_fields = {
        "implementing": ("implement_task_id",),
        "reviewing": ("reviewer_a_task_id", "reviewer_b_task_id"),
        "fixing": ("fixer_task_id",),
        "delivering": ("delivery_task_id",),
    }
    expected_profiles = {
        "implement_task_id": "executor_profile",
        "reviewer_a_task_id": "reviewer_a_profile",
        "reviewer_b_task_id": "reviewer_b_profile",
        "fixer_task_id": "fixer_profile",
        "delivery_task_id": "executor_profile",
    }
    fields = phase_fields.get(prior)
    if not fields:
        raise ValueError(f"factory state {prior!r} is not retryable")

    replacements: dict[str, str] = {}
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
        ).fetchone()
        if (
            current is None or current["state"] != "blocked"
            or current["blocked_from_state"] != prior
        ):
            raise ValueError("factory retry was already claimed by another controller")
        attempt = int(current["retry_attempt"] or 0) + 1
        for field in fields:
            task_id = current[field]
            task = kb.get_task(conn, task_id) if task_id else None
            if task is None:
                raise ValueError(f"phase field {field} has no recoverable task")
            expected_profile = current[expected_profiles[field]]
            expected_toolsets = (
                ["factory_review_readonly"] if prior == "reviewing" else None
            )
            if task.status in _TERMINAL:
                if prior == "reviewing":
                    verdict, _findings, verdict_error = _review_verdict(
                        conn, task.id, expected_profile, current["candidate_sha"]
                    )
                    if verdict_error is None and verdict is not None:
                        continue
                if prior == "delivering":
                    retry_contract = (
                        "Complete with candidate_sha, non-empty tests_run, git_status, "
                        "commit_sha, and delivery.kind exactly "
                        f"{current['delivery_mode']!r}."
                    )
                elif prior == "reviewing":
                    retry_contract = (
                        "Complete with verdict, the exact candidate_sha, findings, and "
                        "non-empty verification metadata."
                    )
                else:
                    retry_contract = (
                        "Complete with inspection_only=false plus non-empty changed_files "
                        "and tests_run metadata."
                    )
                body = (
                    f"{task.body or ''}\n\n"
                    f"Factory retry attempt {attempt}. The prior terminal receipt was "
                    "rejected by the deterministic controller: "
                    f"{current['last_error'] or 'invalid receipt'}. "
                    "Preserve the approved workspace bytes and role boundary. Complete this "
                    f"replacement phase using this exact metadata contract: {retry_contract}"
                )
                replacement = kb.create_task(
                    conn,
                    title=f"{task.title} (retry {attempt})",
                    body=body,
                    assignee=expected_profile,
                    created_by=root_id,
                    workspace_kind="dir",
                    workspace_path=task.workspace_path,
                    tenant=task.tenant,
                    priority=task.priority,
                    idempotency_key=(
                        f"factory:{root_id}:retry:{prior}:{attempt}:{field}"
                    ),
                    max_runtime_seconds=task.max_runtime_seconds,
                    max_retries=task.max_retries,
                    goal_mode=False,
                    initial_status="running",
                    session_id=task.session_id,
                    project_id=task.project_id,
                    worker_toolsets=(
                        ["factory_review_readonly"] if prior == "reviewing" else None
                    ),
                )
                replacements[field] = replacement
            elif task.status in {"blocked", "scheduled"}:
                conn.execute(
                    "UPDATE tasks SET assignee=?,worker_toolsets=?,skills=NULL,"
                    "model_override=NULL,provider_override=NULL,reasoning_effort=NULL "
                    "WHERE id=? AND status IN ('blocked','scheduled')",
                    (
                        expected_profile,
                        json.dumps(expected_toolsets) if expected_toolsets else None,
                        task.id,
                    ),
                )
                if not kb.unblock_task(conn, task.id, allow_nested=True):
                    raise ValueError(f"could not unblock phase {task.id}")
            elif task.status == "triage":
                raise ValueError(f"phase {task.id} is in triage and needs manual repair")
            elif (
                task.assignee != expected_profile
                or task.worker_toolsets != expected_toolsets
            ):
                if task.status == "running":
                    raise ValueError(
                        f"running phase {task.id} has a role or capability mismatch; "
                        "terminate it before retry"
                    )
                conn.execute(
                    "UPDATE tasks SET assignee=?,worker_toolsets=?,skills=NULL,"
                    "model_override=NULL,provider_override=NULL,reasoning_effort=NULL "
                    "WHERE id=? AND status IN ('ready','todo','review')",
                    (
                        expected_profile,
                        json.dumps(expected_toolsets) if expected_toolsets else None,
                        task.id,
                    ),
                )

        assignments = [
            "state=?", "blocked_from_state=NULL", "last_error=NULL",
            "retry_attempt=?", "updated_at=?",
        ]
        values: list[Any] = [prior, attempt, int(time.time())]
        for field, replacement in replacements.items():
            assignments.append(f"{field}=?")
            values.append(replacement)
        values.append(root_id)
        conn.execute(
            f"UPDATE factory_workflows SET {','.join(assignments)} WHERE root_id=?",
            values,
        )
        _stage(conn, root_id, prior)
        kb._append_event(conn, root_id, "factory_retried", {
            "state": prior,
            "attempt": attempt,
            "replacement_tasks": replacements,
        })
    return reconcile_factory(conn, root_id)


def _promote_reviews(conn, row, receipt: dict) -> dict[str, Any]:
    if receipt.get("inspection_only") is not False:
        return _set_error(conn, row["root_id"], "implementation receipt must set inspection_only=false")
    for field in ("changed_files", "tests_run"):
        value = receipt.get(field)
        if not isinstance(value, list) or not value:
            return _set_error(conn, row["root_id"], f"implementation receipt requires non-empty {field}")
    implement = kb.get_task(conn, row["implement_task_id"])
    if implement is None or not implement.workspace_path:
        return _set_error(conn, row["root_id"], "implementation workspace path is missing")
    try:
        candidate, bundle, observed = _candidate_bundle(
            implement.workspace_path, row["root_id"], int(row["cycle"]),
            row["delivery_base_sha"], row["plan_snapshot"], row["plan_sha256"],
        )
    except Exception as exc:
        return _set_error(conn, row["root_id"], f"candidate capture failed: {exc}")
    if not observed and not receipt.get("commit_sha"):
        return _set_error(conn, row["root_id"], "implementation has no observed diff or commit receipt")
    if row["delivery_mode"] == "draft_pr":
        try:
            repo, base, base_sha = _workspace_delivery_target(
                Path(implement.workspace_path).expanduser().resolve()
            )
        except Exception as exc:
            return _set_error(conn, row["root_id"], f"delivery target verification failed: {exc}")
        if (
            repo != row["delivery_repo"] or base != row["delivery_base"]
            or base_sha != row["delivery_base_sha"]
        ):
            return _set_error(
                conn, row["root_id"],
                "workspace delivery target changed after factory intake",
            )
    now = int(time.time())
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT * FROM factory_workflows WHERE root_id=?",
            (row["root_id"],),
        ).fetchone()
        if (
            current is None
            or current["state"] != row["state"]
            or current["implement_task_id"] != row["implement_task_id"]
            or int(current["cycle"]) != int(row["cycle"])
        ):
            return dict(current) if current is not None else {
                "root_id": row["root_id"], "state": "missing",
            }
        a = _create_review_task(
            conn, row, profile=row["reviewer_a_profile"], label="A",
            candidate=candidate, bundle=bundle,
        )
        b = _create_review_task(
            conn, row, profile=row["reviewer_b_profile"], label="B",
            candidate=candidate, bundle=bundle,
        )
        conn.execute(
            "UPDATE factory_workflows SET state='reviewing',candidate_sha=?,"
            "review_bundle_path=?,reviewer_a_task_id=?,reviewer_b_task_id=?,"
            "last_error=NULL,updated_at=? WHERE root_id=?",
            (candidate, bundle, a, b, now, row["root_id"]),
        )
        _stage(conn, row["root_id"], "reviewing")
        kb._append_event(
            conn, row["root_id"], "factory_review_started",
            {"cycle": row["cycle"], "candidate_sha": candidate,
             "reviewer_a_task_id": a, "reviewer_b_task_id": b},
        )
    return inspect_factory(conn, row["root_id"])


def _review_verdict(conn, task_id: str, profile: str, candidate: str) -> tuple[Optional[str], list, Optional[str]]:
    task = kb.get_task(conn, task_id)
    if task is None:
        return None, [], "review task is missing"
    if task.worker_toolsets != ["factory_review_readonly"]:
        return None, [], "review task did not run with the required read-only toolset"
    receipt, error = _phase_receipt(conn, task_id, profile)
    if error or receipt is None:
        return None, [], error
    if receipt.get("candidate_sha") != candidate:
        return None, [], "review receipt is bound to the wrong candidate"
    verdict = receipt.get("verdict")
    if verdict not in {"approve", "changes"}:
        return None, [], "review verdict must be approve or changes"
    findings = receipt.get("findings")
    if not isinstance(findings, list):
        return None, [], "review findings must be a list"
    verification = receipt.get("verification")
    if not isinstance(verification, list) or not verification:
        return None, [], "review verification must be a non-empty list"
    return verdict, findings, None


def _create_fixer(conn, row, findings: list) -> str:
    root = kb.get_task(conn, row["root_id"])
    implement = kb.get_task(conn, row["implement_task_id"])
    return kb.create_task(
        conn,
        title=f"Fix review findings: {root.title if root else row['root_id']}",
        body=(
            f"Factory root: {row['root_id']}\nApproved plan SHA-256: {row['plan_sha256'] or 'legacy-unbound'}\n"
            f"Rejected candidate: {row['candidate_sha']}\n\n"
            f"Findings:\n{json.dumps(findings, indent=2)}\n\n"
            "You are the only writer after review. Address every finding, run local "
            "gates, and do not call request-review: the factory creates new reviewer "
            "cards. Call complete with inspection_only=false plus non-empty "
            "changed_files and tests_run metadata. Do not review your own repair."
        ),
        assignee=row["fixer_profile"],
        created_by=row["root_id"],
        initial_status="running",
        workspace_kind="dir",
        workspace_path=implement.workspace_path if implement else None,
        tenant=root.tenant if root else None,
        project_id=root.project_id if root else None,
        idempotency_key=f"factory:{row['root_id']}:cycle:{row['cycle']}:fix",
        goal_mode=False,
    )


def _create_delivery(conn, row) -> str:
    root = kb.get_task(conn, row["root_id"])
    implement = kb.get_task(conn, row["implement_task_id"])
    return kb.create_task(
        conn,
        title=f"Finalize delivery: {root.title if root else row['root_id']}",
        body=(
        f"Factory root: {row['root_id']}\nApproved plan SHA-256: {row['plan_sha256'] or 'legacy-unbound'}\n"
        f"Approved candidate: {row['candidate_sha']}\n"
        f"Authorized delivery mode: {row['delivery_mode']}\n\n"
            "Do not change the approved implementation. Run the final local gate and "
            "prepare the task's authorized delivery (for example a draft PR when the "
            "root requests one). Complete with candidate_sha, tests_run, git_status, "
            "commit_sha, and a non-empty delivery object whose kind is exactly the "
            "authorized delivery mode shown above. If delivery is not authorized, block."
        ),
        assignee=row["executor_profile"],
        created_by=row["root_id"],
        initial_status="running",
        workspace_kind="dir",
        workspace_path=implement.workspace_path if implement else None,
        tenant=root.tenant if root else None,
        project_id=root.project_id if root else None,
        idempotency_key=f"factory:{row['root_id']}:cycle:{row['cycle']}:delivery",
        goal_mode=False,
    )


def _github_repo_slug(remote_url: str) -> Optional[str]:
    value = str(remote_url or "").strip()
    if value.startswith("git@github.com:"):
        path = value.split(":", 1)[1]
    elif value.startswith(("https://github.com/", "ssh://git@github.com/")):
        marker = "github.com/"
        path = value.split(marker, 1)[1]
    else:
        return None
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    return "/".join(parts[:2]).casefold() if len(parts) >= 2 else None


def _verify_draft_pr_delivery(
    delivery: dict, head: str, expected_repo: str, expected_base: str,
) -> Optional[str]:
    """Verify a claimed draft PR against the code host's read-only API."""
    url = str(delivery.get("url") or "")
    if not (url.startswith("https://github.com/") and "/pull/" in url):
        return "draft_pr delivery requires a GitHub pull URL"
    pr_repo = _github_repo_slug(url.split("/pull/", 1)[0])
    if pr_repo != expected_repo:
        return "draft_pr repository does not match factory intake"
    try:
        repo_probe = subprocess.run(
            ["gh", "repo", "view", expected_repo, "--json", "defaultBranchRef"],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        repository = json.loads(repo_probe.stdout)
        probe = subprocess.run(
            [
                "gh", "pr", "view", url, "--json",
                "url,state,isDraft,headRefOid,baseRefName",
            ],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        observed = json.loads(probe.stdout)
    except Exception as exc:
        return f"draft_pr delivery could not be verified: {exc}"
    default_ref = repository.get("defaultBranchRef")
    observed_default = default_ref.get("name") if isinstance(default_ref, dict) else None
    if observed_default != expected_base:
        return "factory base branch no longer matches code-host default"
    if observed.get("url") != url:
        return "draft_pr URL does not match the code-host receipt"
    if observed.get("state") != "OPEN" or observed.get("isDraft") is not True:
        return "draft_pr delivery is not an open draft"
    if observed.get("baseRefName") != expected_base:
        return "draft_pr base branch does not match factory intake"
    if observed.get("headRefOid") != head or delivery.get("head_sha") != head:
        return "draft_pr head does not match delivered HEAD"
    return None


def _finish_root(conn, row) -> dict[str, Any]:
    """Idempotently finish a root from its already-validated receipt."""
    raw = row["final_receipt"]
    try:
        receipt = json.loads(raw) if raw else None
    except (TypeError, json.JSONDecodeError):
        receipt = None
    if not isinstance(receipt, dict) or receipt.get("contract") != "hermes.factory.receipt.v1":
        return _set_error(conn, row["root_id"], "completing state has no valid final receipt")
    root = kb.get_task(conn, row["root_id"])
    if root is None:
        return _set_error(conn, row["root_id"], "factory root is missing")
    if root.status == "done":
        if root.result != raw:
            return _set_error(conn, row["root_id"], "root was completed with a non-factory receipt")
    else:
        if not kb.complete_task(
            conn, row["root_id"], result=raw, summary="Factory delivery verified",
            metadata=receipt, fire_lifecycle_hook=True, _factory_controller=True,
        ):
            # Another reconciler may have won the root CAS between our read
            # and complete_task(). Treat an exact factory receipt as the same
            # successful terminal transition, never as a reason to regress a
            # verified root into blocked state.
            observed = kb.get_task(conn, row["root_id"])
            if observed is None or observed.status != "done" or observed.result != raw:
                return _set_error(conn, row["root_id"], "verified root completion failed")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE factory_workflows SET state='done',updated_at=? WHERE root_id=?",
            (int(time.time()), row["root_id"]),
        )
        _stage(conn, row["root_id"], "done")
    return inspect_factory(conn, row["root_id"])


def reconcile_factory(conn, root_id: str) -> dict[str, Any]:
    """Advance one workflow using only durable phase receipts."""
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)).fetchone()
    if row is None:
        raise ValueError(f"factory root {root_id!r} not found")
    state = row["state"]
    if state in {"blocked", "done"}:
        return inspect_factory(conn, root_id)
    if state == "completing":
        return _finish_root(conn, row)

    if state == "implementing":
        receipt, error = _phase_receipt(conn, row["implement_task_id"], row["executor_profile"])
        if error:
            return _set_error(conn, root_id, error)
        return _promote_reviews(conn, row, receipt) if receipt is not None else inspect_factory(conn, root_id)

    if state == "fixing":
        receipt, error = _phase_receipt(conn, row["fixer_task_id"], row["fixer_profile"])
        if error:
            return _set_error(conn, root_id, error)
        if receipt is None:
            return inspect_factory(conn, root_id)
        now = int(time.time())
        lost_race = False
        with kb.write_txn(conn):
            current = conn.execute(
                "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
            ).fetchone()
            if (
                current is None or current["state"] != "fixing"
                or current["fixer_task_id"] != row["fixer_task_id"]
                or int(current["cycle"]) != int(row["cycle"])
            ):
                lost_race = True
            else:
                conn.execute(
                    "UPDATE factory_workflows SET state='implementing',cycle=cycle+1,"
                    "implement_task_id=fixer_task_id,reviewer_a_task_id=NULL,"
                    "reviewer_b_task_id=NULL,fixer_task_id=NULL,candidate_sha=NULL,"
                    "review_bundle_path=NULL,updated_at=? WHERE root_id=?",
                    (now, root_id),
                )
                _stage(conn, root_id, "implementing")
        if lost_race:
            return inspect_factory(conn, root_id)
        promoted = conn.execute(
            "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
        ).fetchone()
        return _promote_reviews(conn, promoted, receipt)

    if state == "reviewing":
        va, fa, ea = _review_verdict(
            conn, row["reviewer_a_task_id"], row["reviewer_a_profile"], row["candidate_sha"]
        )
        vb, fb, eb = _review_verdict(
            conn, row["reviewer_b_task_id"], row["reviewer_b_profile"], row["candidate_sha"]
        )
        if ea:
            return _set_error(conn, root_id, f"review A: {ea}")
        if eb:
            return _set_error(conn, root_id, f"review B: {eb}")
        if va is None or vb is None:
            return inspect_factory(conn, root_id)
        findings = fa + fb
        if va == "changes" or vb == "changes":
            now = int(time.time())
            with kb.write_txn(conn):
                current = conn.execute(
                    "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
                ).fetchone()
                if (
                    current is None or current["state"] != "reviewing"
                    or current["candidate_sha"] != row["candidate_sha"]
                ):
                    return dict(current) if current is not None else {
                        "root_id": root_id, "state": "missing",
                    }
                fix = _create_fixer(conn, row, findings)
                conn.execute(
                    "UPDATE factory_workflows SET state='fixing',fixer_task_id=?,updated_at=? WHERE root_id=?",
                    (fix, now, root_id),
                )
                _stage(conn, root_id, "fixing")
                kb._append_event(conn, root_id, "factory_changes_requested", {
                    "candidate_sha": row["candidate_sha"], "fixer_task_id": fix,
                    "findings": findings,
                })
            return inspect_factory(conn, root_id)
        now = int(time.time())
        with kb.write_txn(conn):
            current = conn.execute(
                "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
            ).fetchone()
            if (
                current is None or current["state"] != "reviewing"
                or current["candidate_sha"] != row["candidate_sha"]
            ):
                return dict(current) if current is not None else {
                    "root_id": root_id, "state": "missing",
                }
            delivery = _create_delivery(conn, row)
            conn.execute(
                "UPDATE factory_workflows SET state='delivering',delivery_task_id=?,updated_at=? WHERE root_id=?",
                (delivery, now, root_id),
            )
            _stage(conn, root_id, "delivering")
            kb._append_event(conn, root_id, "factory_quorum_approved", {
                "candidate_sha": row["candidate_sha"], "delivery_task_id": delivery,
            })
        return inspect_factory(conn, root_id)

    if state == "delivering":
        receipt, error = _phase_receipt(conn, row["delivery_task_id"], row["executor_profile"])
        if error:
            return _set_error(conn, root_id, error)
        if receipt is None:
            return inspect_factory(conn, root_id)
        required = ("candidate_sha", "tests_run", "git_status", "commit_sha", "delivery")
        missing = [key for key in required if not receipt.get(key)]
        if missing:
            return _set_error(conn, root_id, f"delivery receipt is missing: {', '.join(missing)}")
        if receipt["candidate_sha"] != row["candidate_sha"]:
            return _set_error(conn, root_id, "delivery receipt changed the approved candidate")
        implement = kb.get_task(conn, row["implement_task_id"])
        if implement is None or not implement.workspace_path:
            return _set_error(conn, root_id, "delivery workspace path is missing")
        workspace = Path(implement.workspace_path).expanduser().resolve()
        try:
            observed_candidate = _workspace_tree_sha(workspace)
        except Exception as exc:
            return _set_error(conn, root_id, f"delivery candidate verification failed: {exc}")
        if observed_candidate != row["candidate_sha"]:
            return _set_error(conn, root_id, "workspace bytes changed after reviewer quorum")
        porcelain = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=workspace, text=True,
            capture_output=True, check=True, timeout=30,
        ).stdout.strip()
        if porcelain or str(receipt["git_status"]).strip().lower() != "clean":
            return _set_error(conn, root_id, "delivery workspace is not clean")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True,
            capture_output=True, check=True, timeout=30,
        ).stdout.strip()
        if receipt["commit_sha"] != head:
            return _set_error(conn, root_id, "delivery commit_sha does not match HEAD")
        head_tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=workspace, text=True,
            capture_output=True, check=True, timeout=30,
        ).stdout.strip()
        if head_tree != row["candidate_sha"]:
            return _set_error(conn, root_id, "delivered commit tree differs from reviewer quorum")
        if not isinstance(receipt["delivery"], dict) or not receipt["delivery"]:
            return _set_error(conn, root_id, "delivery receipt must be a non-empty object")
        delivery = receipt["delivery"]
        if delivery.get("kind") != row["delivery_mode"]:
            return _set_error(conn, root_id, "delivery receipt does not match authorized mode")
        if row["delivery_mode"] == "draft_pr":
            try:
                repo, base, base_sha = _workspace_delivery_target(workspace)
            except Exception as exc:
                return _set_error(
                    conn, root_id, f"delivery target verification failed: {exc}"
                )
            if (
                repo != row["delivery_repo"] or base != row["delivery_base"]
                or base_sha != row["delivery_base_sha"]
            ):
                return _set_error(
                    conn, root_id,
                    "workspace delivery target changed after factory intake",
                )
            draft_error = _verify_draft_pr_delivery(
                delivery, head, row["delivery_repo"], row["delivery_base"]
            )
            if draft_error:
                return _set_error(conn, root_id, draft_error)
        summary = json.dumps({
            "contract": "hermes.factory.receipt.v1",
            "plan_sha256": row["plan_sha256"],
            "candidate_sha": row["candidate_sha"],
            "reviewer_a": row["reviewer_a_profile"],
            "reviewer_b": row["reviewer_b_profile"],
            "delivery": receipt["delivery"],
            "delivery_mode": row["delivery_mode"],
            "commit_sha": receipt["commit_sha"],
            "tests_run": receipt["tests_run"],
        }, sort_keys=True)
        now = int(time.time())
        lost_race = False
        with kb.write_txn(conn):
            current = conn.execute(
                "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
            ).fetchone()
            if (
                current is None or current["state"] != "delivering"
                or current["delivery_task_id"] != row["delivery_task_id"]
                or current["candidate_sha"] != row["candidate_sha"]
            ):
                lost_race = True
            else:
                conn.execute(
                    "UPDATE factory_workflows SET state='completing',final_receipt=?,updated_at=? WHERE root_id=?",
                    (summary, now, root_id),
                )
                _stage(conn, root_id, "completing")
        if lost_race:
            return inspect_factory(conn, root_id)
        completing = conn.execute(
            "SELECT * FROM factory_workflows WHERE root_id=?", (root_id,)
        ).fetchone()
        return _finish_root(conn, completing)

    return _set_error(conn, root_id, f"unknown factory state {state!r}")


def reconcile_all(conn) -> list[dict[str, Any]]:
    ensure_schema(conn)
    roots = [r["root_id"] for r in conn.execute(
        "SELECT root_id FROM factory_workflows WHERE state NOT IN ('blocked','done') ORDER BY created_at"
    )]
    return [reconcile_factory(conn, root) for root in roots]
