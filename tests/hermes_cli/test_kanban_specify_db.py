"""Tests for kb.specify_triage_task — the DB-layer atomic promotion
from the triage column to todo. LLM-free by design."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
    )


def test_specify_promotes_triage_to_todo(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough idea")
        assert kb.get_task(conn, tid).status == "triage"
    with kb.connect() as conn:
        ok = kb.specify_triage_task(
            conn,
            tid,
            title="Refined: rough idea",
            body="**Goal**\nDo the thing.",
            author="specifier-bot",
        )
    assert ok is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # No parents → recompute_ready should have flipped it past todo to ready.
    assert task.status == "ready"
    assert task.title == "Refined: rough idea"
    assert "**Goal**" in (task.body or "")


def test_specify_rejects_blank_title(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough")
    with kb.connect() as conn, pytest.raises(ValueError):
        kb.specify_triage_task(conn, tid, title="   ", body="ok")


def test_specify_records_audit_comment_only_when_author_given(kanban_home):
    # With author → comment added.
    with kb.connect() as conn:
        tid1 = _create_triage(conn, title="a")
        kb.specify_triage_task(
            conn, tid1, title="A-spec", body="b", author="ace"
        )
        comments1 = kb.list_comments(conn, tid1)
    assert len(comments1) == 1
    assert "Specified" in comments1[0].body
    assert comments1[0].author == "ace"

    # Without author → no comment (silent).
    with kb.connect() as conn:
        tid2 = _create_triage(conn, title="b")
        kb.specify_triage_task(conn, tid2, title="B-spec", body="b")
        comments2 = kb.list_comments(conn, tid2)
    assert comments2 == []


def test_specify_can_hold_managed_root_out_of_dispatch(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="rough managed idea",
            triage=True,
            assignee="executor",
            workflow_template_id=kb.GUARDED_WORK_ROOT_TEMPLATE,
            current_step_key="intake",
        )
        assert kb.specify_triage_task(
            conn,
            tid,
            body="**Goal**\nShip safely.",
            hold_in_triage=True,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.assignee is None
        assert task.current_step_key == "planned"


def test_hold_in_triage_rejects_an_ordinary_task(kanban_home):
    with kb.connect() as conn:
        task_id = _create_triage(conn, title="ordinary idea")
        with pytest.raises(ValueError, match="reserved for managed work roots"):
            kb.specify_triage_task(
                conn,
                task_id,
                body="not a factory plan",
                hold_in_triage=True,
            )
        assert kb.get_task(conn, task_id).current_step_key is None


def test_managed_root_cannot_be_marked_planned_with_an_empty_body(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="empty managed idea",
            body="",
            triage=True,
            workflow_template_id=kb.GUARDED_WORK_ROOT_TEMPLATE,
            current_step_key="intake",
        )
        with pytest.raises(ValueError, match="plan body cannot be blank"):
            kb.specify_triage_task(conn, task_id, hold_in_triage=True)
        assert kb.get_task(conn, task_id).current_step_key == "intake"
